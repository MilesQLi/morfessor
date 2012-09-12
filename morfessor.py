#!/usr/bin/python
"""
Morfessor Baseline 2.0

Copyright (c) 2012 Sami Virpioja
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions
are met:

1.  Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.

2.  Redistributions in binary form must reproduce the above
    copyright notice, this list of conditions and the following
    disclaimer in the documentation and/or other materials provided
    with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.

----------------------------------------------------------------------

Contact: Sami Virpioja <sami.virpioja@aalto.fi>

"""

# Corpus contains "compounds" (e.g. words or sentences)
# Smallest pieces are "atoms" (e.g. characters or words)
# Lexicon contains "items" (e.g. morphs or phrases)

__version__ = '2.0'

import math
import random
import cPickle
import os
import time
import re
import sys
import gzip
#import numpy
#import scipy
#import scipy.stats
import array
import itertools
import datetime

class Error(Exception):
    """Base class for exceptions in this module."""
    pass

class InputFormatError(Error):
    """Exception raised for problems in reading input files.

    Attributes:
        file -- input file in which the error occurred
        line -- line that caused the error

    """
    def __init__(self, filename, line):
        self.file = filename
        self.line = line

    def __str__(self):
        return "illegal format in file '%s'" % self.file

_verboselevel = 1

def _vprint(s, l = 1, maxl = 999):
    """Internal function for printing to standard error stream"""
    global _verboselevel
    if _verboselevel >= l and _verboselevel <= maxl:
        sys.stderr.write(s)

_log2pi = math.log(2*math.pi)

def logfactorial(n):
    """Calculate logarithm of n!.

    For large n (n > 20), use Stirling's approximation.

    """
    if n < 2:
        return 0.0
    if n < 20:
        return math.log(math.factorial(n))
    logn = math.log(n)
    return n * logn - n + 0.5 * (logn + _log2pi)

def frequency_distribution_cost(types, tokens):
    """Calculate -log[(M - 1)! (N - M)! / (N - 1)!] for M types and N tokens."""
    if types < 2:
        return 0.0
    return logfactorial(tokens-1) - logfactorial(types-1) - \
        logfactorial(tokens-types)

class Lexicon:
    """Lexicon class for storing model items."""

    def __init__(self):
        """Initialize a new lexicon instance."""
        self.size = 0
        self.dict = {}
        self.atoms = {}
        self.atoms_total = 0
        self.logtokensum = 0.0

    def add(self, item, d = True):
        """Add item to the lexicon (with optional data)."""
        for atom in itertools.chain(item, [None]):
            if not atom in self.atoms:
                oldc = 0
            else:
                oldc = self.atoms[atom]
            self.logtokensum += (oldc+1) * math.log(oldc+1)
            if oldc > 0:
                self.logtokensum -= oldc * math.log(oldc)
            self.atoms[atom] = oldc + 1
        self.atoms_total += len(item) + 1
        self.dict[item] = d
        self.size += 1

    def remove(self, item):
        """Remove item from the lexicon."""
        for atom in itertools.chain(item, [None]):
            oldc = self.atoms[atom]
            self.logtokensum -= oldc * math.log(oldc)
            if oldc > 1:
                self.logtokensum += (oldc-1) * math.log(oldc-1)
            self.atoms[atom] = oldc - 1
            if self.atoms[atom] == 0:
                del self.atoms[atom]
        self.atoms_total -= len(item) + 1
        del self.dict[item]
        self.size -= 1

    def has_item(self, item):
        """Check if lexicon has the item."""
        return self.dict.has_key(item)

    def get_items(self):
        """Return a list of the items in the lexicon."""
        return self.dict.keys()

    def get_value(self, item):
        """Return data stored for the given item."""
        return self.dict[item]

    def set_value(self, item, v):
        """Reset the stored data for the given item."""
        self.dict[item] = v

    def get_cost(self):
        """Return the current coding cost of the lexicon."""
        if self.atoms_total < 2:
            return 0.0
        cost = frequency_distribution_cost(len(self.atoms), self.atoms_total)
        cost += self.atoms_total * math.log(self.atoms_total) - \
            self.logtokensum
        return cost

    def get_codelength(self, item):
        """Return an approximate codelength for new item."""
        l = len(item) + 1
        cost = l * math.log(self.atoms_total + l)
        for atom in itertools.chain(item, [None]):
            if atom in self.atoms:
                c = self.atoms[atom]
            else:
                c = 1
            cost -= math.log(c)
        return cost


class BaselineModel:
    """Morfessor Baseline model class."""

    def __init__(self, forcesplit_list = [], corpusweight = 1.0,
                 annotations = None, supervisedcorpusweight = None,
                 use_skips = False):
        """Initialize a new model instance.

        Arguments:
            forcesplit_list -- force segmentations on the characters in
                               the given list
            corpusweight -- weight for the corpus cost
            annotations -- annotated data for semi-supervised training
            supervisedcorpusweight -- weight for annotated corpus cost; if
                                      None, determine based on data sizes
            use_skips -- randomly skip frequently occurring items to speed
                         up training

        """
        self.analyses = {}
        self.lexicon = Lexicon()
        self.tokens = 0            # Num. of item tokens in corpus
        self.types = 0             # Num. of item types in lexicon size
        self.boundaries = 0        # Compound boundary tokens in corpus
        self.logtokensum = 0.0     # Unnormalized coding length of the corpus
        self.freqdistrcost = 0.0   # Coding cost of frequencies
        self.corpuscost = 0.0      # Code length of pointers in corpus
        self.permutationcost = 0.0 # Code length reduction from permutations
        self.lexiconcost = 0.0     # Code length of lexical items
        self.use_skips = use_skips # Use random skips for frequent items
        if self.use_skips:
            self.counter = {}       # Counter for random skipping
        self.corpuscostweight = corpusweight
        self.forcesplit_list = forcesplit_list
        if annotations != None:
            self.set_annotations(annotations, supervisedcorpusweight)
        else:
            self.supervised = False

    def get_lexicon(self):
        """Return current lexicon instance."""
        return self.lexicon

    def set_annotations(self, annotations, supervisedcorpusweight):
        """Prepare model for semi-supervised learning with given annotations."""
        self.supervised = True
        self.annotations = annotations
        self.superviseditems = {} # {item: count}
        self.supervisedtokens = 0
        self.supervisedlogtokensum = 0.0
        self.supervisedcorpuscost = 0.0
        if supervisedcorpusweight == None:
            self.supervisedcorpusweight = 1.0
            self.sweightbalance = True
        else:
            self.supervisedcorpusweight = supervisedcorpusweight
            self.sweightbalance = False
        self.penaltylogprob = -9999.9 # cost for missing a known item

    def load_segmentations(self, segfile):
        """Load model from existing segmentations in the given file.

        The format of the input file should be that of the Morfessor
        1.0 software. I.e., each line stores one compound as:

        <count> <item1> + <item2> + ... + <itemN>

        """
        if segfile[-3:] == '.gz':
            fobj = gzip.open(segfile, 'r')
        else:
            fobj = open(segfile, 'r')

        for line in fobj:
            if re.search('^#', line):
                continue
            m = re.search('^[ \t]*([0-9]+)[ \t](.+)$', line)
            if not m:
                raise InputFormatError(segfile, line)
            count = int(m.group(1))
            comp = m.group(2)
            items = comp.split(' + ')
            comp = "".join(items)
            self.add(comp, count)
            self.set_compound_analysis(comp, items)
        fobj.close()

    def save_segmentations(self, segfile):
        """Save model segmentations into the given file,

        The format of output file follows that of the Morfessor 1.0
        software. I.e., each line stores one compound as:

        <count> <item1> + <item2> + ... + <itemN>

        """
        if segfile[-3:] == '.gz':
            fobj = gzip.open(segfile, 'w')
        else:
            fobj = open(segfile, 'w')
        d = datetime.datetime.now().replace(microsecond = 0)
        fobj.write("# Output from Morfessor Baseline %s, %s\n" %
                   (__version__, d.isoformat(' ')))
        for w in sorted(self.analyses.keys()):
            c = self.analyses[w][0]
            if c > 0:
                # w is a real compound in training data
                items = self.expand_compound(w)
                fobj.write("%s %s\n" % (c, ' + '.join(map(str, items))))
        fobj.close()

    def batch_init(self, corpus, freqthreshold = 1, cfunc = lambda x: x):
        """Initialize the model for batch training.

        Arguments:
            corpus -- corpus instance
            freqthreshold -- discard compounds that occur less than
                             given times in the corpus (default 1)
            cfunc -- function (int -> int) for modifying the counts
                     (defaults to identity function)

        Adds the compounds in the corpus to the model lexicon.

        """
        for i in range(corpus.get_type_count()):
            c = corpus.get_count(i)
            if c < freqthreshold:
                continue
            w = corpus.get_compound_atoms(i)
            self.add(w, cfunc(c))

    def random_split_init(self, corpus, threshold = 0.5):
        """Initialize the model with random splits.

        Arguments:
            corpus -- Corpus object for loading compounds
            threshold -- probability of splitting at each position (default 0.5)

        """
        for i in range(corpus.get_type_count()):
            w = corpus.get_compound_atoms(i)
            if not w in self.analyses:
                continue
            parts = self.random_split(w, threshold)
            self.set_compound_analysis(w, parts)

    def random_split(self, w, threshold):
        """Return a random split for compound.

        Arguments:
            w -- compound to split
            threshold -- probability of splitting at each position

        """
        parts = []
        startpos = 0
        for i in range(1, len(w)):
            if random.random() < threshold:
                parts.append(w[startpos:i])
                startpos = i
        parts.append(w[startpos:len(w)])
        return parts

    def set_compound_analysis(self, w, parts):
        """Set analysis of compound to according to given segmentation.

        Arguments:
            w -- compound to split
            parts -- items of the compound to use

        The analysis is stored internally as a right-branching tree.

        """
        item = w
        for p in range(len(parts)-1):
            wcount, mcount = self.remove(item)
            prefix = parts[p]
            suffix = reduce(lambda x, y: x + y, parts[p+1:])
            self.analyses[item] = array.array('i', [wcount, mcount,
                                                    len(prefix)])
            self.modify_item_count(prefix, mcount)
            self.modify_item_count(suffix, mcount)
            item = suffix

    def add(self, w, c):
        """Add compound w with count c."""
        self.modify_item_count(w, c)
        self.analyses[w][0] += c
        self.boundaries += c

    def remove(self, item):
        """Remove item from model."""
        wcount, mcount, splitloc = self.analyses[item]
        self.modify_item_count(item, -mcount)
        return wcount, mcount

    def expand_compound(self, w):
        """Return a list containing the analysis of compound w."""
        return self.expand_item(w)

    def expand_item(self, item):
        """Return a list containing the analysis of the existing item."""
        wcount, mcount, splitloc = self.analyses[item]
        items = []
        if splitloc > 0:
            prefix = item[:splitloc]
            suffix = item[splitloc:]
            items += self.expand_item(prefix)
            items += self.expand_item(suffix)
        else:
            items.append(item)
        return items

    def get_item_count(self, item):
        """Return the count of the item."""
        return self.analyses[item][1]

    def get_cost(self):
        """Return current model cost."""
        if self.types == 0:
            return 0.0
        self.permutationcost = -logfactorial(self.types)
        self.freqdistrcost = frequency_distribution_cost(self.types,
                                                         self.tokens)
        n = self.tokens + self.boundaries
        self.corpuscost = self.corpuscostweight * \
            (n * math.log(n) - self.logtokensum -
             self.boundaries * math.log(self.boundaries))
        self.lexiconcost = self.lexicon.get_cost()
        if self.supervised:
            b = self.annotations.get_types()
            if b > 0:
                self.supervisedcorpuscost = self.supervisedcorpusweight * \
                    ((self.supervisedtokens + b) * math.log(n) -
                     self.supervisedlogtokensum -
                     b * math.log(self.boundaries))
            else:
                self.supervisedcorpuscost = 0.0
            return self.permutationcost + self.freqdistrcost + \
                self.lexiconcost + self.corpuscost + self.supervisedcorpuscost
        else:
            return self.permutationcost + self.freqdistrcost + \
                self.lexiconcost + self.corpuscost

    def update_supervised_choices(self):
        """Update the selection of alternative analyses in annotations.

        For semi-supervised models, select the most likely alternative
        analyses included in the annotations of the compounds.

        """
        if not self.supervised:
            return
        # Clean up everything just to be safe
        self.superviseditems = {}
        self.supervisedtokens = 0
        self.supervisedlogtokensum = 0.0
        self.supervisedcorpuscost = 0.0
        # Add to self.supervisedmorphs
        for w, alternatives in self.annotations.get_compounds():
            if w in self.analyses:
                c = self.analyses[w][0]
            else:
                # Add compound also to the unannotated data
                self.add(w, 1)
                c = 1
            analysis, cost = self.best_analysis(alternatives)
            for m in analysis:
                if self.superviseditems.has_key(m):
                    self.superviseditems[m] += c
                else:
                    self.superviseditems[m] = c
                self.supervisedtokens += c
        self.supervisedlogtokensum = 0.0
        for m, f in self.superviseditems.items():
            if self.analyses.has_key(m) and self.analyses[m][2] == 0:
                self.supervisedlogtokensum += f * math.log(self.analyses[m][1])
            else:
                self.supervisedlogtokensum += f * self.penaltylogprob
        if self.tokens > 0:
            n = self.tokens + self.boundaries
            b = self.annotations.get_types() # boundaries in annotated data
            self.supervisedcorpuscost = self.supervisedcorpusweight * \
                ((self.supervisedtokens + b) * math.log(n) -
                 self.supervisedlogtokensum -
                 b * math.log(self.boundaries))
        else:
            self.supervisedcorpuscost = 0.0

    def best_analysis(self, choices):
        """Select the best analysis out of the given choices."""
        bestcost = None
        bestanalysis = None
        for analysis in choices:
            cost = 0.0
            for m in analysis:
                if self.analyses.has_key(m) and self.analyses[m][2] == 0:
                    cost += math.log(self.tokens) - \
                        math.log(self.analyses[m][1])
                else:
                    cost -= self.penaltylogprob # penaltylogprob is negative
            if bestcost == None or cost < bestcost:
                bestcost = cost
                bestanalysis = analysis
        return bestanalysis, bestcost

    def optimize(self, item):
        """Optimize segmentation by recursive splitting.

        Returns list of segments.
        """
        if len(item) == 1: # Single atom
            return [item]

        if self.use_skips:
            if item in self.counter:
                t = self.counter[item]
                if random.random() > 1.0/(max(1, t)):
                    return self.expand_item(item)
                self.counter[item] += 1
            else:
                self.counter[item] = 1

        if item[0] in self.forcesplit_list:
            wcount, mcount = self.remove(item)
            self.analyses[item] = array.array('i', [wcount, mcount, 1])
            self.modify_item_count(item[:1], mcount)
            self.modify_item_count(item[1:], mcount)
            return [item[0]] + self.optimize(item[1:])

        wcount, mcount = self.remove(item)
        self.modify_item_count(item, mcount)
        mincost = self.get_cost()
        self.modify_item_count(item, -mcount)
        splitloc = 0
        for i in range(1, len(item)):
            if item[i] in self.forcesplit_list:
                splitloc = i
                break
            prefix = item[:i]
            suffix = item[i:]
            self.modify_item_count(prefix, mcount)
            self.modify_item_count(suffix, mcount)
            cost = self.get_cost()
            self.modify_item_count(prefix, -mcount)
            self.modify_item_count(suffix, -mcount)
            if cost <= mincost:
                mincost = cost
                splitloc = i
        if splitloc > 0:
            # Virtual item
            self.analyses[item] = array.array('i', [wcount, mcount, splitloc])
            prefix = item[:splitloc]
            suffix = item[splitloc:]
            self.modify_item_count(prefix, mcount)
            self.modify_item_count(suffix, mcount)
            lp = self.optimize(prefix)
            if suffix != prefix:
                return lp + self.optimize(suffix)
            else:
                return lp + lp
        else:
            # Real item
            self.analyses[item] = array.array('i', [wcount, 0, 0])
            self.modify_item_count(item, mcount)
            return [item]

    def modify_item_count(self, item, dcount):
        """Modify the count of item by dcount.

        Adds or removes item to/from lexicon when necessary.

        """
        if self.analyses.has_key(item):
            wcount, mcount, splitloc = self.analyses[item]
        else:
            wcount, mcount, splitloc = array.array('i', [0, 0, 0])
        newmcount = mcount + dcount
        if newmcount == 0:
            del self.analyses[item]
        else:
            self.analyses[item] = array.array('i', [wcount, newmcount,
                                                    splitloc])
        if splitloc > 0:
            # Virtual item
            prefix = item[:splitloc]
            suffix = item[splitloc:]
            self.modify_item_count(prefix, dcount)
            self.modify_item_count(suffix, dcount)
        else:
            # Real item
            self.tokens += dcount
            if mcount > 1:
                self.logtokensum -= mcount * math.log(mcount)
                if self.supervised and self.superviseditems.has_key(item):
                    self.supervisedlogtokensum -= \
                        self.superviseditems[item] * math.log(mcount)
            if newmcount > 1:
                self.logtokensum += newmcount * math.log(newmcount)
                if self.supervised and self.superviseditems.has_key(item):
                    self.supervisedlogtokensum += \
                        self.superviseditems[item] * math.log(newmcount)
            if mcount == 0 and newmcount > 0:
                self.lexicon.add(item)
                self.types += 1
                if self.supervised and self.superviseditems.has_key(item):
                    self.supervisedlogtokensum -= \
                        self.superviseditems[item] * self.penaltylogprob
            elif mcount > 0 and newmcount == 0:
                self.lexicon.remove(item)
                self.types -= 1
                if self.supervised and self.superviseditems.has_key(item):
                    self.supervisedlogtokensum += \
                        self.superviseditems[item] * self.penaltylogprob

    def epoch_update(self, epoch_num):
        """Do model updates that are necessary between training epochs.

        The argument is the number of training epochs finished.

        In practice, this does two things:
        - If random skipping is in use, reset item counters.
        - If semi-supervised learning is in use and there are alternative
          analyses in the annotated data, select the annotations that are
          most likely given the model parameters. If not hand-set, update
          the weight of the annotated corpus.

        This method should also be run prior to training (with the
        epoch number argument as 0).

        """
        if self.use_skips:
            self.counter = {}
        if self.supervised:
            self.update_supervised_choices()
            if self.sweightbalance and epoch_num == 0:
                # Set the corpus cost weight of annotated data
                # according to the ratio of compound tokens in the
                # data sets
                self.supervisedcorpusweight = self.corpuscostweight * \
                    float(self.boundaries) / self.annotations.get_types()
                _vprint("Corpus weight of annotated data set to %s\n" %
                        self.supervisedcorpusweight, 2)

    def get_viterbi_segments(self, compound, allow_new_items = True):
        """Find optimal segmentation using the Viterbi algorithm."""
        clen = len(compound)
        grid = [(0.0, None)]
        logtokens = math.log(self.tokens)
        badlikelihood = clen * logtokens
        # Viterbi main loop
        for t in range(1, clen+1):
            # Select the best path to current node.
            # Note that we can come from any node in history.
            bestpath = None
            bestcost = None
            for pt in range(0, t):
                if grid[pt][0] == None:
                    continue
                cost = grid[pt][0]
                item = compound[pt:t]
                if self.analyses.has_key(item) and \
                       self.analyses[item][2] == 0:
                    if self.analyses[item][1] < 1:
                        raise StandardError("count of %s is %s" % (item, self.analyses[item][1]))
                    cost += logtokens - math.log(self.analyses[item][1])
                elif allow_new_items:
                    cost -= self.types * logtokens
                    cost += (self.types+1) * math.log(self.tokens+1)
                    cost += self.lexicon.get_codelength(item)
                elif len(item) == 1:
                    cost += badlikelihood
                else:
                    continue
                if bestcost == None or cost < bestcost:
                    bestcost = cost
                    bestpath = pt
            grid.append((bestcost, bestpath))
        items = []
        path = grid[-1][1]
        lt = clen + 1
        while path != None:
            t = path
            items.append(compound[t:lt])
            path = grid[t][1]
            lt = t
        items.reverse()
        return items, bestcost

class Corpus:
    """Class for storing text corpus as a list of compound objects."""

    def __init__(self, atom_sep = None):
        """Initialize a new corpus instance.

        Arguments:
            atom_sep -- regular expression for splitting a compound
                        (e.g. word or sentence) to its atoms (e.g. characters
                        or words). If None (default), split to letters.

        """
        self.files = []
        self.atom_sep = atom_sep
        self.types = 0
        self.tokens = 0
        self.compounds = []
        self.counts = []
        self.strdict = {}
        self.text = []
        self.max_clen = 0

    def get_token_count(self):
        """Return the total number of compounds in the corpus."""
        return self.tokens

    def get_type_count(self):
        """Return the number of compound types in the corpus."""
        return self.types

    def get_compound_str(self, i):
        """Return the string representation of the compound at index i."""
        return self.compounds[i]

    def get_compound_atoms(self, i):
        """Return the atom representation of the compound at index i."""
        if self.atom_sep == None:
            return self.compounds[i] # string
        else:
            return tuple(re.split(self.atom_sep, self.compounds[i])) # tuple

    def get_count(self, i):
        """Return the count of of the compound at index i."""
        return self.counts[i]

    def get_counts(self):
        """Return the list of counts of the compounds."""
        return self.counts

    def has_compound(self, c):
        """Check whether the corpus has given compound.

        The input can be either a string or a list/tuple of strings.
        """
        if type(c) == str:
            return (c in self.strdict)
        else:
            return (reduce(lambda x,y: x+y, c) in self.strdict)

    def get_text(self):
        """Return the compound indices of the text of the corpus."""
        return self.text

    def get_max_compound_len(self):
        """Return the maximum of the lenghts of the compounds in the corpus."""
        return self.max_clen

    def get_compound_len(self, c):
        """Return the number of atoms in the compound."""
        if self.atom_sep == None:
            return len(c)
        else:
            return len(re.split(self.atom_sep, c))

    def load(self, datafile, compound_sep = ' *', comment_re = "^#"):
        """Load corpus from file.

        Arguments:
            datafile -- filename
            compound_sep -- regexp for separating compounds
            comment_re -- regexp for comment lines

        """
        self.files.append((datafile, 'corpus', compound_sep, comment_re))
        if datafile == '-':
            fobj = sys.stdin
        elif datafile[-3:] == '.gz':
            fobj = gzip.open(datafile, 'r')
        else:
            fobj = open(datafile, 'r')

        for line in fobj:
            if re.search(comment_re, line):
                continue
            if compound_sep == None or compound_sep == '':
                # Line is one compound
                compounds = [line.rstrip()]
            else:
                # Line can have several compounds
                compounds = re.split(compound_sep, line.rstrip())
            linetext = []
            for comp in compounds:
                if comp == '':
                    continue
                if comp in self.strdict:
                    i = self.strdict[comp]
                    self.counts[i] += 1
                else:
                    i = self.types
                    self.strdict[comp] = i
                    self.compounds.append(comp)
                    self.counts.append(1)
                    self.types += 1
                    self.max_clen = max(self.max_clen,
                                        self.get_compound_len(comp))
                self.tokens += 1
                linetext.append(i)
            self.text.append(linetext)

        if datafile != '-':
            fobj.close()

    def generator(self, datafiles, compound_sep = ' *', comment_re = "^#"):
        """Return a iterator for the compounds in a set of corpora.

        Arguments:
            datafiles -- a list of filenames
            compound_sep -- regexp for separating compounds (default ' *')
            comment_re -- regexp for comment lines (default '^#')

        """
        for datafile in datafiles:
            self.files.append((datafile, compound_sep, comment_re))
            if datafile == '-':
                fobj = sys.stdin
            elif datafile[-3:] == '.gz':
                fobj = gzip.open(datafile, 'r')
            else:
                fobj = open(datafile, 'r')

            for line in fobj:
                if re.search(comment_re, line):
                    continue
                if compound_sep == None or compound_sep == '':
                    # Line is one compound
                    compounds = [line.rstrip()]
                else:
                    # Line can have several compounds
                    compounds = re.split(compound_sep, line.rstrip())
                linetext = []
                for comp in compounds:
                    if comp == '':
                        continue
                    if comp in self.strdict:
                        i = self.strdict[comp]
                        self.counts[i] += 1
                    else:
                        i = self.types
                        self.strdict[comp] = i
                        self.compounds.append(comp)
                        self.counts.append(1)
                        self.types += 1
                        self.max_clen = max(self.max_clen,
                                            self.get_compound_len(comp))
                    self.tokens += 1
                    linetext.append(i)
                    yield self.get_compound_atoms(i)
                self.text.append(linetext)
            if datafile != '-':
                fobj.close()

    def load_from_list(self, datafile, comment_re = "^#"):
        """Load data from a file that contains a list of compounds.

        Arguments:
            datafile -- filename
            comment_re -- regexp for comment lines (default '^#')

        Each line of the datafile should contain one compound,
        optionally preceeded by an integer count. (If the count is not
        available, it is assumed to be one.)
        """
        self.files.append((datafile, 'list', comment_re))
        if datafile == '-':
            fobj = sys.stdin
        elif datafile[-3:] == '.gz':
            fobj = gzip.open(datafile, 'r')
        else:
            fobj = open(datafile, 'r')
        for line in fobj:
            if re.search(comment_re, line):
                continue
            m = re.search('^([0-9]+) +(.*)$', line)
            if m:
                count = int(m.group(1))
                comp = m.group(2)
            else:
                count = 1
                comp = line.rstrip()
            if comp == '':
                continue
            if comp in self.strdict:
                i = self.strdict[comp]
                self.counts[i] += count
            else:
                i = self.types
                self.strdict[comp] = i
                self.compounds.append(comp)
                self.counts.append(count)
                self.types += 1
                self.max_clen = max(self.max_clen,
                                    self.get_compound_len(comp))
            self.tokens += count
        if datafile != '-':
            fobj.close()


class Annotations:
    """Annotated data for semi-supervised learning."""

    def __init__(self):
        """Initialize a new instance of annotated data."""
        self.types = 0
        self.analyses = {}

    def get_types(self):
        """Return the number of annotated compound types."""
        return self.types

    def get_compounds(self):
        """Return the annotated compounds."""
        return self.analyses.items()

    def has_analysis(self, compound):
        """Return whether the given compound has annotation."""
        return compound in self.analyses

    def get_analyses(self, compound):
        """Return the analyses for the given compound."""
        return self.analyses[compound]

    def load(self, datafile, separator = ' ', altseparator = ', '):
        """Load annotations from file.

        Arguments:
            datafile -- filename
            separator -- regexp for separating items in one analysis
            comment_re -- regexp for separating alternative analyses

        """
        if datafile[-3:] == '.gz':
            fobj = gzip.open(datafile, 'r')
        else:
            fobj = open(datafile, 'r')
        for line in fobj:
            try:
                compound, analyses_part = line.split("\t")
            except ValueError:
                raise InputFormatError(datafile, line)
            alt_analyses = analyses_part.split(altseparator)
            analyses = []
            for i in range(len(alt_analyses)):
                analyses.append(alt_analyses[i].split(separator))
            self.analyses[compound] = analyses
            self.types += 1
        fobj.close()


def batch_train(model, corpus, freqthreshold = 1, finishthreshold = 0.005):
    """Do batch training for a Morfessor model.

    Arguments:
        model -- model instance
        corpus -- corpus instance
        freqthreshold -- discard compounds that occur less than given
                          times in corpus (default 1)
        finishthreshold -- finish training after the decrease in cost
                           per compound is smaller than the threshold
                           (default 0.005)

    The model should already include all the compounds in the corpus
    (run model.batch_init(corpus) beforehand).

    """
    model.epoch_update(0)
    oldcost = 0.0
    newcost = model.get_cost()
    wordstoprocess = len(filter(lambda x: x >= freqthreshold,
                                corpus.get_counts()))
    _vprint("Found %s compounds in training data\n" % wordstoprocess, 1)
    dotfreq = int(math.ceil(wordstoprocess / 70.0))
    epochs = 0
    _vprint("Starting batch training\n", 1)
    _vprint("Epochs: %s\tCost: %s\n" % (epochs, newcost), 1)
    while True:
        # One epoch
        indices = range(corpus.get_type_count())
        random.shuffle(indices)
        i = 0
        for j in indices:
            if corpus.get_count(j) < freqthreshold:
                continue
            w = corpus.get_compound_atoms(j)
            segments = model.optimize(w)
            _vprint("#%s: %s\n" % (i, segments), 2)
            i += 1
            if i % dotfreq == 0:
                _vprint(".", 1, 1)
        epochs += 1
        _vprint("\n", 1, 1)
        _vprint("Cost before epoch update: %s\n" % model.get_cost(), 2)
        model.epoch_update(epochs)
        oldcost = newcost
        newcost = model.get_cost()
        _vprint("Epochs: %s\tCost: %s\n" % (epochs, newcost), 1)
        if epochs > 1 and newcost >= oldcost - finishthreshold * wordstoprocess:
            break
    _vprint("Done.\n", 1)
    return epochs, newcost

def online_train(model, corpusiter, epochinterval = 10000, dampfunc = None):
    """Do on-line training for a Morfessor model.

    Arguments:
        model -- model instance
        corpusiter -- iterator over corpus
        epochinterval -- run model.epoch_update() after every n:th word
                         (default 10000)
        dampfunc -- function for dampening the compound frequencies

    """
    model.epoch_update(0)
    if dampfunc != None:
        counts = {}
    _vprint("Starting online training\n", 1)
    i = 0
    epochs = 0
    dotfreq = int(math.ceil(epochinterval / 70.0))
    for w in corpusiter:
        if dampfunc != None:
            if not counts.has_key(w):
                c = 0
                counts[w] = 1
                addc = 1
            else:
                c = counts[w]
                counts[w] = c + 1
                addc = dampfunc(c+1) - dampfunc(c)
            if addc > 0:
                model.add(w, addc)
        else:
            model.add(w, 1)
        segments = model.optimize(w)
        _vprint("#%s: %s\n" % (i, segments), 2)
        i += 1
        if i % dotfreq == 0:
                _vprint(".", 1, 1)
        if i % epochinterval == 0:
            _vprint("\n", 1, 1)
            epochs += 1
            model.epoch_update(epochs)
            newcost = model.get_cost()
            _vprint("Tokens processed: %s\tCost: %s\n" % (i, newcost), 1)
    epochs += 1
    model.epoch_update(epochs)
    newcost = model.get_cost()
    _vprint("\nTokens processed: %s\tCost: %s\n" % (i, newcost), 1)
    return epochs, newcost

def corpus_segmentation_dict(model, corpus):
    """Find the most likely segmentations for the compounds in corpus.

    Arguments:
        model -- model instance to use in Viterbi search
        corpus -- corpus instance

    Returns a dictionary that maps each compound to its segmentation.

    """
    d = {}
    for i in range(corpus.get_type_count()):
        s = corpus.get_compound_str(i)
        w = corpus.get_compound_atoms(i)
        items, logp = model.get_viterbi_segments(w)
        d[s] = items
    return d

def main(argv):
    import argparse

    parser = argparse.ArgumentParser(
        prog = 'morfessor.py',
        description="""
Morfessor Baseline %s

Copyright (c) 2012 Sami Virpioja
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions
are met:

1.  Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.

2.  Redistributions in binary form must reproduce the above
    copyright notice, this list of conditions and the following
    disclaimer in the documentation and/or other materials provided
    with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
""" %  __version__,
        epilog = """
Simple usage examples (training and testing):

  %(prog)s -t training_corpus.txt -s model.pickled
  %(prog)s -l model.pickled -T test_corpus.txt -o test_corpus.segmented

Interactive use (read corpus from user):

  %(prog)s -m online -v 2 -t -

""",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-a', '--annotations', dest="annofile", default=None,
                        help="load annotated data for semi-supervised "+
                        "learning", metavar='<file>')
    parser.add_argument('-b', '--break', dest="separator", type=str,
                        default=None, metavar='<regexp>',
                        help="atom separator regexp (default %(default)s)")
    parser.add_argument('-c', '--compbreak', dest="cseparator", type=str,
                        default=' +', metavar='<regexp>',
                        help="compound separator regexp "+
                        "(default '%(default)s')")
    parser.add_argument('-C', '--compoundlistdata', dest="list", default=False,
                        action='store_true',
                        help="input file(s) for batch training are lists "+
                        "(one compound per line, optionally count as prefix)")
    parser.add_argument('-d', '--dampening', dest="dampening", type=str,
                        default='none', metavar='<type>',
                        help="frequency dampening for training data ("+
                        "'none', 'log', or 'ones'; default '%(default)s')")
    parser.add_argument('-e', '--epochinterval', dest="epochinterval", type=int,
                        default=10000, metavar='<int>',
                        help="epoch interval for online training ("+
                        "default %(default)s)")
    parser.add_argument('-f', '--forcesplit', dest="forcesplit", type=list,
                        default=['-'], metavar='<list>',
                        help="force split on given atoms (default %(default)s)")
    parser.add_argument('-F', '--freqthreshold', dest="freqthreshold", type=int,
                        default=1, metavar='<int>',
                        help="frequency threshold for batch training ("+
                        "default %(default)s)")
    parser.add_argument('-l', '--load', dest="loadfile", default=None,
                        help="load existing model from file (pickled object)",
                        metavar='<file>')
    parser.add_argument('-L', '--loadsegmentation', dest="loadsegfile",
                        default=None,
                        help="load existing model from segmentation file "+
                        "(Morfessor 1.0 format)", metavar='<file>')
    parser.add_argument('-m', '--mode', dest="trainmode", default='batch',
                        help="training mode ('batch', 'online', or "+
                        "'online+batch'; default '%(default)s')",
                        metavar='<mode>')
    parser.add_argument('-o', '--output', dest="outfile", default='-',
                        help="output file for test data results "+
                        "(for standard output, use '-'; default "+
                        "'%(default)s')",  metavar='<file>')
    parser.add_argument('-q', '--skips', dest="skips", default=False,
                        action='store_true',
                        help="use random skips for frequently seen "+
                        "compounds to speed up training")
    parser.add_argument('-r', '--randseed', dest="randseed", default=None,
                        help="seed for random number generator",
                        metavar='<seed>')
    parser.add_argument('-R', '--randsplit', dest="splitprob", default=None,
                        type = float, metavar = '<float>',
                        help="initialize model by random splitting using "+
                        "the given split probability (default no splitting)")
    parser.add_argument('-s', '--save', dest="savefile", default=None,
                        help="save final model to file (pickled object)",
                        metavar='<file>')
    parser.add_argument('-S', '--savesegmentation', dest="savesegfile",
                        default=None,
                        help="save model segmentations to file "+
                        "(Morfessor 1.0 format)", metavar='<file>')
    parser.add_argument('-t', '--traindata', dest='trainfiles',
                        action='append', default = [],
                        help="input corpus file(s) for training (text or "+
                        "gzipped text; use '-' for standard input; "+
                        "add several times in order to append multiple files)",
                        metavar='<file>')
    parser.add_argument('-T', '--testdata', dest='testfiles',
                        action='append', default = [],
                        help="input corpus file(s) for testing (text or "+
                        "gzipped text;  use '-' for standard input; "+
                        "add several times in order to append multiple files)",
                        metavar='<file>')
    parser.add_argument('-v', '--verbose', dest="verbose", type=int,
                        default=1, help="verbose level; controls what is "+
                        "written to the standard error stream "+
                        "(default %(default)s)", metavar='<int>')
    parser.add_argument('-w', '--corpusweight', dest="corpusweight",
                        type=float, default=1.0, metavar='<float>',
                        help="corpus weight parameter (default %(default)s)")
    parser.add_argument('-W', '--supervisedweight', dest="scorpusweight",
                        type=float, default=None, metavar='<float>',
                        help="corpus weight parameter for annotated data "+
                        "(if unset, the weight is set to balance the "+
                        "costs of annotated and unannotated data sets)")
    parser.add_argument('-x', '--lexicon', dest="lexfile", default=None,
                        help="output final lexicon to given file",
                        metavar='<file>')
    args = parser.parse_args(argv)

    global _verboselevel
    _verboselevel = args.verbose

    if args.loadfile == None and args.loadsegfile == None and \
            len(args.trainfiles) == 0:
        parser.error("either model file or training data should be defined")

    if args.randseed != None:
        random.seed(args.randseed)

    # Load annotated data if specified
    if args.annofile != None:
        annotations = Annotations()
        annotations.load(args.annofile)
    else:
        annotations = None

    # Load exisiting model or create a new one
    if args.loadfile != None:
        _vprint("Loading model from '%s'..." % args.loadfile, 1)
        with open(args.loadfile, 'rb') as fobj:
            model = cPickle.load(fobj)
        _vprint(" Done.\n", 1)
        if annotations != None:
            # Add annotated data to model
            model.set_annotations(annotations, args.scorpusweight)
    elif args.loadsegfile != None:
        _vprint("Loading model from '%s'..." % args.loadsegfile, 1)
        model = BaselineModel(forcesplit_list = args.forcesplit,
                              corpusweight = args.corpusweight,
                              annotations = annotations,
                              supervisedcorpusweight = args.scorpusweight,
                              use_skips = args.skips)
        model.load_segmentations(args.loadsegfile)
        _vprint(" Done.\n", 1)
    else:
        model = BaselineModel(forcesplit_list = args.forcesplit,
                              corpusweight = args.corpusweight,
                              annotations = annotations,
                              supervisedcorpusweight = args.scorpusweight,
                              use_skips = args.skips)

    # Train model
    if len(args.trainfiles) > 0:
        # Set frequency dampening function
        if args.dampening == 'none':
            dampfunc = lambda x: x
        elif args.dampening == 'log':
            dampfunc = lambda x: int(round(math.log(x+1, 2)))
        elif args.dampening == 'ones':
            dampfunc = lambda x: 1
        else:
            parser.error("unknown dampening type '%s'" % args.dampening)
        ts = time.time()
        if args.trainmode == 'batch':
            data = Corpus(args.separator)
            for f in args.trainfiles:
                if f == '-':
                    _vprint("Loading training data from standard "+
                                 "input\n", 1)
                else:
                    _vprint("Loading training data file '%s'..." % f, 1)
                if args.list:
                    data.load_from_list(f)
                else:
                    data.load(f, args.cseparator)
                _vprint(" Done.\n", 1)
            model.batch_init(data, args.freqthreshold, dampfunc)
            if args.splitprob != None:
                model.random_split_init(data, args.splitprob)
            e, c = batch_train(model, data, freqthreshold = args.freqthreshold)
        elif args.trainmode == 'online':
            data = Corpus(args.separator)
            dataiter = data.generator(args.trainfiles, args.cseparator)
            e, c = online_train(model, dataiter, args.epochinterval, dampfunc)
        elif args.trainmode == 'online+batch':
            data = Corpus(args.separator)
            dataiter = data.generator(args.trainfiles, args.cseparator)
            e, c = online_train(model, dataiter, args.epochinterval, dampfunc)
            e, c = batch_train(model, data)
        else:
            parser.error("unknown training mode '%s'" % args.trainmode)
        te = time.time()
        _vprint("Epochs: %s\nFinal cost: %s\nTime: %.3fs\n" %
                     (e, c, te-ts), 1)

    # Save model
    if args.savefile != None:
        _vprint("Saving model to '%s'..." % args.savefile, 1)
        with open(args.savefile, 'wb') as fobj:
            cPickle.dump(model, fobj, cPickle.HIGHEST_PROTOCOL)
        _vprint(" Done.\n", 1)

    if args.savesegfile != None:
        _vprint("Saving model segmentations to '%s'..." %
                     args.savesegfile, 1)
        model.save_segmentations(args.savesegfile)
        _vprint(" Done.\n", 1)

    # Output lexicon
    if args.lexfile != None:
        if args.lexfile == '-':
            fobj = sys.stdout
        elif args.lexfile[-3:] == '.gz':
            fobj = gzip.open(args.lexfile, 'w')
        else:
            fobj = open(args.lexfile, 'w')
        if args.lexfile != '-':
            _vprint("Saving model lexicon to '%s'..." %
                    args.lexfile, 1)
        for item in sorted(model.get_lexicon().get_items()):
            fobj.write("%s %s\n" % (model.get_item_count(item), item))
        if args.lexfile != '-':
            fobj.close()
            _vprint(" Done.\n", 1)

    # Segment test data
    if len(args.testfiles) > 0:
        _vprint("Segmenting test data...", 1)
        if args.outfile == '-':
            fobj = sys.stdout
        elif args.outfile[-3:] == '.gz':
            fobj = gzip.open(args.outfile, 'w')
        else:
            fobj = open(args.outfile, 'w')
        testdata = Corpus(args.separator)
        testdataiter = testdata.generator(args.testfiles, args.cseparator)
        i = 0
        for compound in testdataiter:
            items, logp = model.get_viterbi_segments(compound)
            fobj.write("%s\n" % ' '.join(items))
            i += 1
            if i % 10000 == 0:
                _vprint(".", 1, 1)
        if args.outfile != '-':
            fobj.close()
        _vprint(" Done.\n", 1)

if __name__ == "__main__":
    main(sys.argv[1:])