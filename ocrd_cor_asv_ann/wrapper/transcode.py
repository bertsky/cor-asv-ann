from __future__ import absolute_import

import os
from functools import reduce
import numpy as np

from ocrd import Processor
from ocrd_utils import (
    getLogger,
    assert_file_grp_cardinality,
    make_file_id,
    xywh_from_points,
    points_from_xywh,
    MIMETYPE_PAGE
)
from ocrd_modelfactory import page_from_file
from ocrd_models.ocrd_page import (
    to_xml,
    WordType, CoordsType, TextEquivType
)

from .config import OCRD_TOOL
from ..lib.seq2seq import Sequence2Sequence, GAP

TOOL_NAME = 'ocrd-cor-asv-ann-process'

class ANNCorrection(Processor):
    
    def __init__(self, *args, **kwargs):
        kwargs['ocrd_tool'] = OCRD_TOOL['tools'][TOOL_NAME]
        kwargs['version'] = OCRD_TOOL['version']
        super(ANNCorrection, self).__init__(*args, **kwargs)
        if (not hasattr(self, 'workspace') or not self.workspace or
            not hasattr(self, 'parameter') or not self.parameter):
            # no parameter/workspace for --dump-json or --version (no processing)
            return
        
        if not 'TF_CPP_MIN_LOG_LEVEL' in os.environ:
            os.environ['TF_CPP_MIN_LOG_LEVEL'] = '1'

        def canread(path):
            return os.path.isfile(path) and os.access(path, os.R_OK)
        def getfile(path):
            if canread(path):
                return path
            dirname = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                os.pardir, os.pardir)
            if canread(os.path.join(dirname, path)):
                return os.path.join(dirname, path)
            dirname = os.path.join(dirname, 'models')
            if canread(os.path.join(dirname, path)):
                return os.path.join(dirname, path)
            if 'CORASVANN_DATA' in os.environ:
                dirname = os.environ['CORASVANN_DATA']
                if canread(os.path.join(dirname, path)):
                    return os.path.join(dirname, path)
            raise Exception('Cannot find model_file in path "%s"' % path)
        
        model_file = getfile(self.parameter['model_file'])
        self.s2s = Sequence2Sequence(logger=getLogger('processor.ANNCorrection'), progbars=True)
        self.s2s.load_config(model_file)
        self.s2s.configure()
        self.s2s.load_weights(model_file)
        self.s2s.rejection_threshold = self.parameter['rejection_threshold']
        self.s2s.beam_width_in = self.parameter['fixed_beam_width']
        self.s2s.beam_threshold_in = self.parameter['relative_beam_width']
        
    def process(self):
        """Perform OCR post-correction with encoder-attention-decoder ANN on the workspace.
        
        Open and deserialise PAGE input files, then iterate over the element hierarchy
        down to the requested `textequiv_level`, making sequences of TextEquiv objects
        as lists of lines. Concatenate their string values, obeying rules of implicit
        whitespace, and map the string positions where the objects start.
        
        Next, transcode the input lines into output lines in parallel, and use
        the retrieved soft alignment scores to calculate hard alignment paths
        between input and output string via Viterbi decoding. Then use those
        to map back the start positions and overwrite each TextEquiv with its
        new content, paying special attention to whitespace:
        
        Distribute edits such that whitespace objects cannot become more than whitespace
        (or be deleted) and that non-whitespace objects must not start of end with
        whitespace (but may contain new whitespace in the middle).
        
        Subsequently, unless processing on the `line` level, make the Word segmentation
        consistent with that result again: merge around deleted whitespace tokens and
        split at whitespace inside non-whitespace tokens.
        
        Finally, make the levels above `textequiv_level` consistent with that
        textual result (by concatenation joined by whitespace).
        
        Produce new output files by serialising the resulting hierarchy.
        """
        assert_file_grp_cardinality(self.input_file_grp, 1)
        assert_file_grp_cardinality(self.output_file_grp, 1)
        # Dragging Word/TextLine references along in all lists besides TextEquiv
        # is necessary because the generateDS version of the PAGE-XML model
        # has no references upwards in the hierarchy (from TextEquiv to containing
        # elements, from Glyph/Word/TextLine to Word/TextLine/TextRegion), and
        # its classes are not hashable.
        level = self.parameter['textequiv_level']
        LOG = getLogger('processor.ANNCorrection')
        for n, input_file in enumerate(self.input_files):
            LOG.info("INPUT FILE %i / %s", n, input_file.pageId or input_file.ID)

            pcgts = page_from_file(self.workspace.download_file(input_file))
            page_id = input_file.pageId or input_file.ID # (PageType has no id)
            LOG.info("Correcting text in page '%s' at the %s level", page_id, level)
            
            # annotate processing metadata:
            self.add_metadata(pcgts)
            
            # get textequiv references for all lines:
            # FIXME: conf with TextEquiv alternatives
            line_sequences = _page_get_line_sequences_at(level, pcgts)

            # concatenate to strings and get dict of start positions to refs:
            input_lines, conf, textequiv_starts, word_starts, textline_starts = (
                _line_sequences2string_sequences(self.s2s.mapping[0], line_sequences))
            
            # correct string and get input-output alignment:
            # FIXME: split into self.batch_size chunks
            output_lines, output_probs, output_scores, alignments = (
                self.s2s.correct_lines(input_lines, conf,
                                       fast=self.parameter['fast_mode'],
                                       greedy=self.parameter['fast_mode']))
            
            # re-align (from alignment scores) and overwrite the textequiv references:
            for (input_line, output_line, output_prob,
                 score, alignment,
                 textequivs, words, textlines) in zip(
                     input_lines, output_lines, output_probs,
                     output_scores, alignments,
                     textequiv_starts, word_starts, textline_starts):
                LOG.debug('"%s" -> "%s"', input_line.rstrip('\n'), output_line.rstrip('\n'))
                
                # convert soft scores (seen from output) to hard path (seen from input):
                realignment = _alignment2path(alignment, len(input_line), len(output_line),
                                              1. / self.s2s.voc_size)
                
                # overwrite TextEquiv references:
                new_sequence = _update_sequence(
                    input_line, output_line, output_prob,
                    score, realignment,
                    textequivs, words, textlines)
                
                # update Word segmentation:
                if level != 'line':
                    _resegment_sequence(new_sequence, level)
                
                LOG.info('corrected line with %d elements, ppl: %.3f', len(new_sequence), np.exp(score))
            
            # make higher levels consistent again:
            page_update_higher_textequiv_levels(level, pcgts)
            
            # write back result to new annotation:
            file_id = make_file_id(input_file, self.output_file_grp)
            pcgts.set_pcGtsId(file_id)
            file_path = os.path.join(self.output_file_grp, file_id + '.xml')
            self.workspace.add_file(
                ID=file_id,
                file_grp=self.output_file_grp,
                pageId=input_file.pageId,
                local_filename=file_path,
                mimetype=MIMETYPE_PAGE,
                content=to_xml(pcgts))
            
def _page_get_line_sequences_at(level, pcgts):
    '''Get TextEquiv sequences for PAGE-XML hierarchy level including whitespace.
    
    Return a list of lines from the document `pcgts`,
    where each line is a list of 3-tuples containing
    TextEquiv / Word / TextLine objects from the given
    hierarchy `level`. This includes artificial objects
    for implicit whitespace between elements (marked by
    `index=-1`, which is forbidden in the XML Schema).
    
    (If `level` is `glyph`, then the Word reference
     will be the Word that contains the Glyph which
     contains the TextEquiv.
     If `level` is `word`, then the Word reference
     will be the Word which contains the TextEquiv.
     If `level` is `line`, then the Word reference
     will be None.)
    '''
    LOG = getLogger('processor.ANNCorrection')
    sequences = list()
    word = None # make accessible after loop
    line = None # make accessible after loop
    regions = pcgts.get_Page().get_AllRegions(classes=['Text'], order='reading-order')
    if not regions:
        LOG.warning("Page contains no text regions")
    first_region = True
    for region in regions:
        lines = region.get_TextLine()
        if not lines:
            LOG.warning("Region '%s' contains no text lines", region.id)
            continue
        if not first_region:
            sequences[-1].append((TextEquivType(Unicode='\n', conf=1.0, index=-1), word, line))
        first_region = False
        first_line = True
        for line in lines:
            if not first_line:
                sequences[-1].append((TextEquivType(Unicode='\n', conf=1.0, index=-1), word, line))
            sequences.append([])
            first_line = False
            if level == 'line':
                #LOG.debug("Getting text in line '%s'", line.id)
                textequivs = line.get_TextEquiv()
                if not textequivs:
                    LOG.warning("Line '%s' contains no text results", line.id)
                    continue
                sequences[-1].append((textequivs[0], word, line))
                continue
            words = line.get_Word()
            if not words:
                LOG.warning("Line '%s' contains no word", line.id)
                continue
            first_word = True
            for word in words:
                if not first_word:
                    sequences[-1].append((TextEquivType(Unicode=' ', conf=1.0, index=-1), word, line))
                first_word = False
                if level == 'word':
                    #LOG.debug("Getting text in word '%s'", word.id)
                    textequivs = word.get_TextEquiv()
                    if not textequivs:
                        LOG.warning("Word '%s' contains no text results", word.id)
                        continue
                    sequences[-1].append((textequivs[0], word, line))
                    continue
                glyphs = word.get_Glyph()
                if not glyphs:
                    LOG.warning("Word '%s' contains no glyphs", word.id)
                    continue
                for glyph in glyphs:
                    #LOG.debug("Getting text in glyph '%s'", glyph.id)
                    textequivs = glyph.get_TextEquiv()
                    if not textequivs:
                        LOG.warning("Glyph '%s' contains no text results", glyph.id)
                        continue
                    sequences[-1].append((textequivs[0], word, line))
    if sequences:
        sequences[-1].append((TextEquivType(Unicode='\n', conf=1.0, index=-1), word, line))
    # filter empty lines (containing only newline):
    return [line for line in sequences if len(line) > 1]

def _line_sequences2string_sequences(mapping, line_sequences):
    '''Concatenate TextEquiv / Word / TextLine sequences to line strings.
    
    Return a list of line strings, a list of confidence lists,
    a list of dicts from string positions to TextEquiv references,
    a list of dicts from string positions to Word references, and
    a list of dicts from string positions to TextLine references.
    '''
    input_lines, conf, textequiv_starts, word_starts, textline_starts = [], [], [], [], []
    for line_sequence in line_sequences:
        i = 0
        input_lines.append('')
        conf.append(list())
        textequiv_starts.append(dict())
        word_starts.append(dict())
        textline_starts.append(dict())
        for textequiv, word, textline in line_sequence:
            textequiv_starts[-1][i] = textequiv
            word_starts[-1][i] = word
            textline_starts[-1][i] = textline
            j = len(textequiv.Unicode)
            if not textequiv.Unicode:
                # empty element (OCR rejection):
                # this information is still valuable for post-correction,
                # and we reserved index zero for underspecified inputs,
                # therefore here we just need to replace the gap with some
                # unmapped character, like GAP:
                assert GAP not in mapping, (
                    'character "%s" must not be mapped (needed for gap repair)' % GAP)
                textequiv.Unicode = GAP
                j = 1
            input_lines[-1] += textequiv.Unicode
            # generateDS does not convert simpleType for attributes (yet?)
            conf[-1].extend([float(textequiv.conf or "1.0")] * j)
            i += j
    return input_lines, conf, textequiv_starts, word_starts, textline_starts

def _alignment2path(alignment, i_max, j_max, min_score):
    '''Find the best path through a soft alignment matrix via Viterbi search.
    
    The `alignment` is a list of vectors of scores (between 0..1).
    The list indexes are output positions (ignored above `j_max`),
    the vector indexes are input positions (ignored above `i_max`).
    Viterbi forward scores are only calculated where the alignment
    scores are larger than `min_score` (to save time).
    
    Return a dictionary mapping input positions to output positions
    (i.e. a realignment path).
    '''
    # compute Viterbi forward pass:
    viterbi_fw = np.zeros((i_max, j_max), dtype=np.float32)
    i, j = 0, 0
    while i < i_max and j < j_max:
        if i > 0:
            im1 = viterbi_fw[i - 1, j]
        else:
            im1 = 0
        if j > 0:
            jm1 = viterbi_fw[i, j - 1]
        else:
            jm1 = 0
        if i > 0 and j > 0:
            ijm1 = viterbi_fw[i - 1, j - 1]
        else:
            ijm1 = 0
        viterbi_fw[i, j] = alignment[j][i] + max(im1, jm1, ijm1)
        while True:
            i += 1
            if i == i_max:
                j += 1
                if j == j_max:
                    break
                i = 0
            if alignment[j][i] > min_score:
                break
    # compute Viterbi backward pass:
    i = i_max - 1 if i_max <= j_max else j_max - 2 + int(
        np.argmax(viterbi_fw[j_max - i_max - 2:, j_max - 1]))
    j = j_max - 1 if j_max <= i_max else i_max - 2 + int(
        np.argmax(viterbi_fw[i_max - 1, i_max - j_max - 2:]))
    realignment = {i_max: j_max} # init end of line
    while i >= 0 and j >= 0:
        realignment[i] = j # (overwrites any previous assignment)
        if viterbi_fw[i - 1, j] > viterbi_fw[i, j - 1]:
            if viterbi_fw[i - 1, j] > viterbi_fw[i - 1, j - 1]:
                i -= 1
            else:
                i -= 1
                j -= 1
        elif viterbi_fw[i, j - 1] > viterbi_fw[i - 1, j - 1]:
            j -= 1
        else:
            j -= 1
            i -= 1
    realignment[0] = 0 # init start of line
    # LOG.debug('realignment: %s', str(realignment))
    # from matplotlib import pyplot
    # pyplot.imshow(viterbi_fw)
    # pyplot.show()
    return realignment

def _update_sequence(input_line, output_line, output_prob,
                     score, realignment,
                     textequivs, words, textlines):
    '''Apply correction across TextEquiv elements along alignment path of one line.
    
    Traverse the path `realignment` through `input_line` and `output_line`,
    looking up TextEquiv objects by their start positions via `textequivs`.
    Overwrite the string value of the objects (which equals the segment in
    `input_line`) with the corrected version (which equals the segment in
    `output_line`), and overwrite the confidence values from `output_prob`.
    
    Also, redistribute string parts bordering whitespace: make sure space
    only maps to space (or gets deleted, which necessitates merging Words),
    and non-space only maps to non-space (with space allowed only in the
    middle, which necessitates splitting Words). This is required in order
    to avoid loosing content: the implicit whitespace TextEquivs do not
    belong to the document hierarchy itself.
    (Merging and splitting can be done afterwards.)
    
    Return a list of TextEquiv / Word / TextLine tuples thus processed.
    '''
    i_max = len(input_line)
    j_max = len(output_line)
    textequivs.setdefault(i_max, None) # init end of line
    line = next(line for line in textlines.values() if line)
    last = None
    sequence = []
    for i in textequivs:
        if i in realignment:
            j = realignment[i]
        else:
            # this element was deleted
            j = last[1]
        #print(last, [i, j])
        if last:
            input_ = input_line[last[0]:i]
            output = output_line[last[1]:j]
            # try to distribute whitespace onto whitespace, i.e.
            # if input is Whitespace, move any Non-whitespace parts
            # in output to neighbours;
            # otherwise, move Whitespace parts to neighbours
            # if their input is Whitespace too;
            # input:  N|    W    |N   N|     W   |   W|    N    |W
            # output:  |<-N W N->|     |<-W<-N W |    |<-W N W->|
            if input_ in (" ", "\n"):
                if output and not output.startswith((" ", "\n")) and sequence:
                    while output and not output.startswith((" ", "\n")):
                        sequence[-1][0].Unicode += output[0]
                        last[1] += 1
                        output = output[1:]
                    #print('corrected non-whitespace LHS: ', last, [i, j])
                if output and not output.endswith((" ", "\n")):
                    j -= len(output.split(" ")[-1])
                    output = output_line[last[1]:j]
                    #print('corrected non-whitespace RHS: ', last, [i, j])
                if output.split() and sequence:
                    while output.split():
                        sequence[-1][0].Unicode += output[0]
                        last[1] += 1
                        output = output[1:]
                    #print('corrected non-whitespace middle: ', last, [i, j])
            else:
                if output.startswith(" ") and sequence and sequence[-1][0].index == -1:
                    while output.startswith(" "):
                        sequence[-1][0].Unicode += output[0]
                        last[1] += 1
                        output = output[1:]
                    #print('corrected whitespace LHS: ', last, [i, j])
                if output.endswith((" ", "\n")) and i < i_max and input_line[i] in (" ", "\n"):
                    while output.endswith((" ", "\n")):
                        j -= 1
                        output = output[:-1]
                    #print('corrected whitespace RHS: ', last, [i, j])
            textequiv = textequivs[last[0]]
            assert textequiv.Unicode == input_, (
                'source element "%s" does not match input section "%s" in line "%s"' % (
                    textequiv.Unicode, input_, line.id))
            #print("'" + textequiv.Unicode + "' -> '" + output + "'")
            textequiv.Unicode = output
            #textequiv.conf = np.exp(-score)
            prob = output_prob[last[1]:j]
            textequiv.conf = np.mean(prob or [1.0])
            word = words[last[0]]
            textline = textlines[last[0]]
            sequence.append((textequiv, word, textline))
        last = [i, j]
    assert last == [i_max, j_max], (
        'alignment path did not reach top: %d/%d vs %d/%d in line "%s"' % (
            last[0], last[1], i_max, j_max, line.id))
    for i, (textequiv, _, _) in enumerate(sequence):
        assert not textequiv.Unicode.split() or textequiv.index != -1, (
            'output "%s" will be lost at (whitespace) element %d in line "%s"' % (
                textequiv.Unicode, i, line.id))
    return sequence

def _resegment_sequence(sequence, level):
    '''Merge and split Words among `sequence` after correction.
    
    At each empty whitespace TextEquiv, merge the neighbouring Words.
    At each non-whitespace TextEquiv which contains whitespace, split
    the containing Word at the respective positions.
    '''
    LOG = getLogger('processor.ANNCorrection')
    for i, (textequiv, word, textline) in enumerate(sequence):
        if textequiv.index == -1:
            if not textequiv.Unicode:
                # whitespace was deleted: merge adjacent words
                if i == 0 or i == len(sequence) - 1:
                    LOG.error('cannot merge Words at the %s of line "%s"',
                              'end' if i else 'start', textline.id)
                else:
                    prev_textequiv, prev_word, _ = sequence[i - 1]
                    next_textequiv, next_word, _ = sequence[i + 1]
                    if not prev_word or not next_word:
                        LOG.error('cannot merge Words "%s" and "%s" in line "%s"',
                                  prev_textequiv.Unicode, next_textequiv.Unicode, textline.id)
                    else:
                        merged = _merge_words(prev_word, next_word)
                        LOG.debug('merged %s and %s to %s in line %s',
                                  prev_word.id, next_word.id, merged.id, textline.id)
                        textline.set_Word([merged if word is prev_word else word
                                           for word in textline.get_Word()
                                           if not word is next_word])
        elif " " in textequiv.Unicode:
            # whitespace was introduced: split word
            if not word:
                LOG.error('cannot split Word "%s" in line "%s"',
                          textequiv.Unicode, textline.id)
            else:
                if level == 'glyph':
                    glyph = next(glyph for glyph in word.get_Glyph()
                                 if textequiv in glyph.get_TextEquiv())
                    prev_, next_ = _split_word_at_glyph(word, glyph)
                    parts = [prev_, next_]
                else:
                    parts = []
                    next_ = word
                    while True:
                        prev_, next_ = _split_word_at_space(next_)
                        if " " in next_.get_TextEquiv()[0].Unicode:
                            parts.append(prev_)
                        else:
                            parts.append(prev_)
                            parts.append(next_)
                            break
                LOG.debug('split %s to %s in line %s',
                          word.id, [w.id for w in parts], textline.id)
                textline.set_Word(reduce(lambda l, w, key=word, value=parts:
                                         l + value if w is key else l + [w],
                                         textline.get_Word(), []))
    
def _merge_words(prev_, next_):
    merged = WordType(id=prev_.id + '.' + next_.id)
    merged.set_Coords(CoordsType(points=points_from_xywh(xywh_from_points(
        prev_.get_Coords().points + ' ' + next_.get_Coords().points))))
    if prev_.get_language():
        merged.set_language(prev_.get_language())
    if prev_.get_TextStyle():
        merged.set_TextStyle(prev_.get_TextStyle())
    if prev_.get_Glyph() or next_.get_Glyph():
        merged.set_Glyph(prev_.get_Glyph() + next_.get_Glyph())
    if prev_.get_TextEquiv():
        merged.set_TextEquiv(prev_.get_TextEquiv())
    else:
        merged.set_TextEquiv([TextEquivType(Unicode='', conf=1.0)])
    if next_.get_TextEquiv():
        textequiv = merged.get_TextEquiv()[0]
        textequiv2 = next_.get_TextEquiv()[0]
        textequiv.Unicode += textequiv2.Unicode
        if textequiv.conf and textequiv2.conf:
            textequiv.conf *= textequiv2.conf
    return merged

def _split_word_at_glyph(word, glyph):
    prev_ = WordType(id=word.id + '_l')
    next_ = WordType(id=word.id + '_r')
    xywh_glyph = xywh_from_points(glyph.get_Coords().points)
    xywh_word = xywh_from_points(word.get_Coords().points)
    xywh_prev = xywh_word.copy()
    xywh_prev.update({'w': xywh_glyph['x'] - xywh_word['x']})
    prev_.set_Coords(CoordsType(points=points_from_xywh(
        xywh_prev)))
    xywh_next = xywh_word.copy()
    xywh_next.update({'x': xywh_glyph['x'] - xywh_glyph['w'],
                      'w': xywh_word['w'] - xywh_prev['w']})
    next_.set_Coords(CoordsType(points=points_from_xywh(
        xywh_next)))
    if word.get_language():
        prev_.set_language(word.get_language())
        next_.set_language(word.get_language())
    if word.get_TextStyle():
        prev_.set_TextStyle(word.get_TextStyle())
        next_.set_TextStyle(word.get_TextStyle())
    glyphs = word.get_Glyph()
    pos = glyphs.index(glyph)
    prev_.set_Glyph(glyphs[0:pos])
    next_.set_Glyph(glyphs[pos+1:])
    # TextEquiv: will be overwritten by page_update_higher_textequiv_levels
    return prev_, next_

def _split_word_at_space(word):
    prev_ = WordType(id=word.id + '_l')
    next_ = WordType(id=word.id + '_r')
    xywh = xywh_from_points(word.get_Coords().points)
    textequiv = word.get_TextEquiv()[0]
    pos = textequiv.Unicode.index(" ")
    fract = pos / len(textequiv.Unicode)
    xywh_prev = xywh.copy()
    xywh_prev.update({'w': xywh['w'] * fract})
    prev_.set_Coords(CoordsType(points=points_from_xywh(
        xywh_prev)))
    xywh_next = xywh.copy()
    xywh_next.update({'x': xywh['x'] + xywh['w'] * fract,
                      'w': xywh['w'] * (1 - fract)})
    next_.set_Coords(CoordsType(points=points_from_xywh(
        xywh_next)))
    if word.get_language():
        prev_.set_language(word.get_language())
        next_.set_language(word.get_language())
    if word.get_TextStyle():
        prev_.set_TextStyle(word.get_TextStyle())
        next_.set_TextStyle(word.get_TextStyle())
    # Glyphs: irrelevant at this processing level
    textequiv_prev = TextEquivType(Unicode=textequiv.Unicode[0:pos],
                                   conf=textequiv.conf)
    textequiv_next = TextEquivType(Unicode=textequiv.Unicode[pos+1:],
                                   conf=textequiv.conf)
    prev_.set_TextEquiv([textequiv_prev])
    next_.set_TextEquiv([textequiv_next])
    return prev_, next_

def page_update_higher_textequiv_levels(level, pcgts):
    '''Update the TextEquivs of all PAGE-XML hierarchy levels above `level` for consistency.
    
    Starting with the hierarchy level chosen for processing,
    join all first TextEquiv (by the rules governing the respective level)
    into TextEquiv of the next higher level, replacing them.
    '''
    regions = pcgts.get_Page().get_AllRegions(classes=['Text'], order='reading-order')
    if level != 'region':
        for region in regions:
            lines = region.get_TextLine()
            if level != 'line':
                for line in lines:
                    words = line.get_Word()
                    if level != 'word':
                        for word in words:
                            glyphs = word.get_Glyph()
                            word_unicode = u''.join(glyph.get_TextEquiv()[0].Unicode
                                                    if glyph.get_TextEquiv()
                                                    else u'' for glyph in glyphs)
                            word_conf = np.mean([glyph.get_TextEquiv()[0].conf
                                                 if glyph.get_TextEquiv()
                                                 else 1. for glyph in glyphs])
                            word.set_TextEquiv( # remove old
                                [TextEquivType(Unicode=word_unicode,
                                               conf=word_conf)])
                    line_unicode = u' '.join(word.get_TextEquiv()[0].Unicode
                                             if word.get_TextEquiv()
                                             else u'' for word in words)
                    line_conf = np.mean([word.get_TextEquiv()[0].conf
                                         if word.get_TextEquiv()
                                         else 1. for word in words])
                    line.set_TextEquiv( # remove old
                        [TextEquivType(Unicode=line_unicode,
                                       conf=line_conf)])
            region_unicode = u'\n'.join(line.get_TextEquiv()[0].Unicode
                                        if line.get_TextEquiv()
                                        else u'' for line in lines)
            region_conf = np.mean([line.get_TextEquiv()[0].conf
                                   if line.get_TextEquiv()
                                   else 1. for line in lines])
            region.set_TextEquiv( # remove old
                [TextEquivType(Unicode=region_unicode,
                               conf=region_conf)])
