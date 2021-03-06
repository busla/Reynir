"""

    Reynir: Natural language processing for Icelandic

    Tokenizer module

    Copyright (C) 2016 Vilhjálmur Þorsteinsson

       This program is free software: you can redistribute it and/or modify
       it under the terms of the GNU General Public License as published by
       the Free Software Foundation, either version 3 of the License, or
       (at your option) any later version.
       This program is distributed in the hope that it will be useful,
       but WITHOUT ANY WARRANTY; without even the implied warranty of
       MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
       GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see http://www.gnu.org/licenses/.


    The function tokenize() consumes a text string and
    returns a generator of tokens. Each token is a tuple,
    typically having the form (type, word, meaning),
    where type is one of the constants specified in the
    TOK class, word is the original word found in the
    source text, and meaning is a list of tuples with
    potential interpretations of the word, as retrieved
    from the BIN database of word forms.

"""

from contextlib import closing
from collections import namedtuple, defaultdict
from functools import lru_cache

import re
import codecs
import datetime

from settings import Settings, StaticPhrases, Abbreviations, AmbigPhrases, DisallowedNames
from settings import changedlocale
from bindb import BIN_Db, BIN_Meaning
from scraperdb import SessionContext, Entity


# Recognized punctuation

LEFT_PUNCTUATION = "([„«#$€<"
RIGHT_PUNCTUATION = ".,:;)]!%?“»”’…°>–"
CENTER_PUNCTUATION = '"*&+=@©|—'
NONE_PUNCTUATION = "-/'~‘\\"
PUNCTUATION = LEFT_PUNCTUATION + CENTER_PUNCTUATION + RIGHT_PUNCTUATION + NONE_PUNCTUATION

# Punctuation that ends a sentence
END_OF_SENTENCE = frozenset(['.', '?', '!', '[…]'])
# Punctuation symbols that may additionally occur at the end of a sentence
SENTENCE_FINISHERS = frozenset([')', ']', '“', '»', '”', '’', '"', '[…]'])
# Punctuation symbols that may occur inside words
PUNCT_INSIDE_WORD = frozenset(['.', "'", '‘', "´", "’"]) # Period and apostrophes

# Hyphens that are cast to '-' for parsing and then re-cast
# to normal hyphens, en or em dashes in final rendering
HYPHENS = "—–-"
HYPHEN = '-' # Normal hyphen

# Hyphens that may indicate composite words ('fjármála- og efnahagsráðuneyti')
COMPOSITE_HYPHENS = "–-"
COMPOSITE_HYPHEN = '–' # en dash

CLOCK_WORD = "klukkan"
CLOCK_ABBREV = "kl"

# Prefixes that can be applied to adjectives with an intervening hyphen
ADJECTIVE_PREFIXES = frozenset(["hálf", "marg", "semí"])

# Person names that are not recognized at the start of sentences
NOT_NAME_AT_SENTENCE_START = { "Annar" }

# Punctuation types: left, center or right of word

TP_LEFT = 1   # Whitespace to the left
TP_CENTER = 2 # Whitespace to the left and right
TP_RIGHT = 3  # Whitespace to the right
TP_NONE = 4   # No whitespace
TP_WORD = 5   # Flexible whitespace depending on surroundings

# Matrix indicating correct spacing between tokens

TP_SPACE = (
    # Next token is:
    # LEFT    CENTER  RIGHT   NONE    WORD
    # Last token was TP_LEFT:
    ( False,  True,   False,  False,  False),
    # Last token was TP_CENTER:
    ( True,   True,   True,   True,   True),
    # Last token was TP_RIGHT:
    ( True,   True,   False,  False,  True),
    # Last token was TP_NONE:
    ( False,  True,   False,  False,  False),
    # Last token was TP_WORD:
    ( True,   True,   False,  False,  True)
)

# Numeric digits

DIGITS = frozenset([d for d in "0123456789"]) # Set of digit characters

# Set of all cases (nominative, accusative, dative, possessive)

ALL_CASES = frozenset(["nf", "þf", "þgf", "ef"])

# Month names and numbers
MONTHS = {
    "janúar": 1,
    "febrúar": 2,
    "mars": 3,
    "apríl": 4,
    "maí": 5,
    "júní": 6,
    "júlí": 7,
    "ágúst": 8,
    "september": 9,
    "október": 10,
    "nóvember": 11,
    "desember": 12
}

# Named tuple for person names, including case and gender

PersonName = namedtuple('PersonName', ['name', 'gender', 'case'])

# Named tuple for tokens

Tok = namedtuple('Tok', ['kind', 'txt', 'val'])


def correct_spaces(s):
    """ Split and re-compose a string with correct spacing between tokens"""
    r = []
    last = TP_NONE
    for w in s.split():
        if len(w) > 1:
            this = TP_WORD
        elif w in LEFT_PUNCTUATION:
            this = TP_LEFT
        elif w in RIGHT_PUNCTUATION:
            this = TP_RIGHT
        elif w in NONE_PUNCTUATION:
            this = TP_NONE
        elif w in CENTER_PUNCTUATION:
            this = TP_CENTER
        else:
            this = TP_WORD
        if TP_SPACE[last - 1][this - 1] and r:
            r.append(" " + w)
        else:
            r.append(w)
        last = this
    return "".join(r)


# Token types

class TOK:

    # Note: Keep the following in sync with token identifiers in main.js

    PUNCTUATION = 1
    TIME = 2
    DATE = 3
    YEAR = 4
    NUMBER = 5
    WORD = 6
    TELNO = 7
    PERCENT = 8
    URL = 9
    ORDINAL = 10
    TIMESTAMP = 11
    CURRENCY = 12
    AMOUNT = 13
    PERSON = 14
    EMAIL = 15
    ENTITY = 16
    UNKNOWN = 17

    P_BEGIN = 10001 # Paragraph begin
    P_END = 10002 # Paragraph end

    S_BEGIN = 11001 # Sentence begin
    S_END = 11002 # Sentence end

    # Token descriptive names

    descr = {
        PUNCTUATION: "PUNCTUATION",
        TIME: "TIME",
        TIMESTAMP: "TIMESTAMP",
        DATE: "DATE",
        YEAR: "YEAR",
        NUMBER: "NUMBER",
        CURRENCY: "CURRENCY",
        AMOUNT: "AMOUNT",
        PERSON: "PERSON",
        WORD: "WORD",
        UNKNOWN: "UNKNOWN",
        TELNO: "TELNO",
        PERCENT: "PERCENT",
        URL: "URL",
        EMAIL: "EMAIL",
        ORDINAL: "ORDINAL",
        ENTITY: "ENTITY",
        P_BEGIN: "BEGIN PARA",
        P_END: "END PARA",
        S_BEGIN: "BEGIN SENT",
        S_END: "END SENT"
    }

    # Token constructors
    @staticmethod
    def Punctuation(w):
        tp = TP_CENTER # Default punctuation type
        if w:
            if w[0] in LEFT_PUNCTUATION:
                tp = TP_LEFT
            elif w[0] in RIGHT_PUNCTUATION:
                tp = TP_RIGHT
            elif w[0] in NONE_PUNCTUATION:
                tp = TP_NONE
        return Tok(TOK.PUNCTUATION, w, tp)

    @staticmethod
    def Time(w, h, m, s):
        return Tok(TOK.TIME, w, (h, m, s))

    @staticmethod
    def Date(w, y, m, d):
        return Tok(TOK.DATE, w, (y, m, d))

    @staticmethod
    def Timestamp(w, y, mo, d, h, m, s):
        return Tok(TOK.TIMESTAMP, w, (y, mo, d, h, m, s))

    @staticmethod
    def Year(w, n):
        return Tok(TOK.YEAR, w, n)

    @staticmethod
    def Telno(w):
        return Tok(TOK.TELNO, w, None)

    @staticmethod
    def Email(w):
        return Tok(TOK.EMAIL, w, None)

    @staticmethod
    def Number(w, n, cases=None, genders=None):
        """ cases is a list of possible cases for this number
            (if it was originally stated in words) """
        return Tok(TOK.NUMBER, w, (n, cases, genders))

    @staticmethod
    def Currency(w, iso, cases=None, genders=None):
        """ cases is a list of possible cases for this currency name
            (if it was originally stated in words, i.e. not abbreviated) """
        return Tok(TOK.CURRENCY, w, (iso, cases, genders))

    @staticmethod
    def Amount(w, iso, n, cases=None, genders=None):
        """ cases is a list of possible cases for this amount
            (if it was originally stated in words) """
        return Tok(TOK.AMOUNT, w, (n, iso, cases, genders))

    @staticmethod
    def Percent(w, n, cases=None, genders=None):
        return Tok(TOK.PERCENT, w, (n, cases, genders))

    @staticmethod
    def Ordinal(w, n):
        return Tok(TOK.ORDINAL, w, n)

    @staticmethod
    def Url(w):
        return Tok(TOK.URL, w, None)

    @staticmethod
    def Word(w, m):
        """ m is a list of BIN_Meaning tuples fetched from the BÍN database """
        return Tok(TOK.WORD, w, m)

    @staticmethod
    def Unknown(w):
        return Tok(TOK.UNKNOWN, w, None)

    @staticmethod
    def Person(w, m):
        """ m is a list of PersonName tuples: (name, gender, case) """
        return Tok(TOK.PERSON, w, m)

    @staticmethod
    def Entity(w, definitions, cases=None, genders=None):
        return Tok(TOK.ENTITY, w, (definitions, cases, genders))

    @staticmethod
    def Begin_Paragraph():
        return Tok(TOK.P_BEGIN, None, None)

    @staticmethod
    def End_Paragraph():
        return Tok(TOK.P_END, None, None)

    @staticmethod
    def Begin_Sentence(num_parses = 0, err_index = None):
        return Tok(TOK.S_BEGIN, None, (num_parses, err_index))

    @staticmethod
    def End_Sentence():
        return Tok(TOK.S_END, None, None)


def parse_digits(w):
    """ Parse a raw token starting with a digit """

    s = re.match(r'\d{1,2}:\d\d:\d\d', w)
    if s:
        # Looks like a 24-hour clock, H:M:S
        w = s.group()
        p = w.split(':')
        h = int(p[0])
        m = int(p[1])
        sec = int(p[2])
        if (0 <= h < 24) and (0 <= m < 60) and (0 <= sec < 60):
            return TOK.Time(w, h, m, sec), s.end()
    s = re.match(r'\d{1,2}:\d\d', w)
    if s:
        # Looks like a 24-hour clock, H:M
        w = s.group()
        p = w.split(':')
        h = int(p[0])
        m = int(p[1])
        if (0 <= h < 24) and (0 <= m < 60):
            return TOK.Time(w, h, m, 0), s.end()
    s = re.match(r'\d{1,2}\.\d{1,2}\.\d{2,4}', w) or re.match(r'\d{1,2}/\d{1,2}/\d{2,4}', w)
    if s:
        # Looks like a date
        w = s.group()
        if '/' in w:
            p = w.split('/')
        else:
            p = w.split('.')
        y = int(p[2])
        # noinspection PyAugmentAssignment
        if y <= 99:
            y = 2000 + y
        m = int(p[1])
        d = int(p[0])
        if m > 12 >= d:
            # Probably wrong way around
            m, d = d, m
        if (1776 <= y <= 2100) and (1 <= m <= 12) and (1 <= d <= 31):
            return TOK.Date(w, y, m, d), s.end()
    s = re.match(r'\d+(\.\d\d\d)*,\d+', w)
    if s:
        # Real number formatted with decimal comma and possibly thousands separator
        # (we need to check this before checking integers)
        w = s.group()
        n = re.sub(r'\.', '', w) # Eliminate thousands separators
        n = re.sub(r',', '.', n) # Convert decimal comma to point
        return TOK.Number(w, float(n)), s.end()
    s = re.match(r'\d+(\.\d\d\d)+', w)
    if s:
        # Integer with a '.' thousands separator
        # (we need to check this before checking dd.mm dates)
        w = s.group()
        n = re.sub(r'\.', '', w) # Eliminate thousands separators
        return TOK.Number(w, int(n)), s.end()
    s = re.match(r'\d{1,2}/\d{1,2}', w) or re.match(r'\d{1,2}\.\d{1,2}', w)
    if s:
        # Looks like a date
        w = s.group()
        if '/' in w:
            p = w.split('/')
        else:
            p = w.split('.')
        m = int(p[1])
        d = int(p[0])
        if '/' in w:
            if p[0][0] != '0' and p[1][0] != '0' and ((d <= 5 and m <= 6) or (d == 1 and m <= 10)):
                # This is probably a fraction, not a date
                # (1/2, 1/3, 1/4, 1/5, 1/6, 2/3, 2/5, 5/6 etc.)
                # Return a number
                return TOK.Number(w, float(d) / m), s.end()
        else:
            # We have what looks like DD.MM (or MM.DD)
            # However, this might just as well be a time.
            # If it is a valid time, assume so.
            if 0 <= d <= 23 and 0 <= m <= 59:
                return TOK.Time(w, d, m, 0), s.end()
        if m > 12 >= d:
            # Date is probably wrong way around
            m, d = d, m
        if (1 <= d <= 31) and (1 <= m <= 12):
            # Looks like a (roughly) valid date
            return TOK.Date(w, 0, m, d), s.end()
    s = re.match(r'\d\d\d\d$', w) or re.match(r'\d\d\d\d[^\d]', w)
    if s:
        n = int(w[0:4])
        if 1776 <= n <= 2100:
            # Looks like a year
            return TOK.Year(w[0:4], n), 4
    s = re.match(r'\d\d\d-\d\d\d\d', w) or re.match(r'\d\d\d\d\d\d\d', w)
    if s:
        # Looks like a telephone number
        return TOK.Telno(s.group()), s.end()
    s = re.match(r'\d+(,\d\d\d)*\.\d+', w)
    if s:
        # Real number, possibly with a thousands separator and decimal comma/point
        w = s.group()
        n = re.sub(r',', '', w) # Eliminate thousands separators
        return TOK.Number(w, float(n)), s.end()
    s = re.match(r'\d+(,\d\d\d)*', w)
    if s:
        # Integer, possibly with a ',' thousands separator
        w = s.group()
        n = re.sub(r',', '', w) # Eliminate thousands separators
        return TOK.Number(w, int(n)), s.end()
    # Strange thing
    return TOK.Unknown(w), len(w)


def parse_tokens(txt):
    """ Generator that parses contiguous text into a stream of tokens """

    rough = txt.split()

    for w in rough:
        # Handle each sequence of non-whitespace characters

        if w.isalpha():
            # Shortcut for most common case: pure word
            yield TOK.Word(w, None)
            continue

        # More complex case of mixed punctuation, letters and numbers
        if len(w) > 1 and w[0] == '"':
            # Convert simple quotes to proper opening quotes
            yield TOK.Punctuation('„')
            w = w[1:]

        while w:
            # Punctuation
            ate = False
            while w and w[0] in PUNCTUATION:
                ate = True
                if w.startswith("[...]"):
                    yield TOK.Punctuation("[…]")
                    w = w[5:]
                elif w.startswith("[…]"):
                    yield TOK.Punctuation("[…]")
                    w = w[3:]
                elif w.startswith("..."):
                    # Treat ellipsis as one piece of punctuation
                    yield TOK.Punctuation("…")
                    w = w[3:]
                elif w.startswith(",,"):
                    # Probably an idiot trying to type opening double quotes with commas
                    yield TOK.Punctuation('„')
                    w = w[2:]
                elif len(w) == 2 and (w == "[[" or w == "]]"):
                    # Begin or end paragraph marker
                    if w == "[[":
                        yield TOK.Begin_Paragraph()
                    else:
                        yield TOK.End_Paragraph()
                    w = w[2:]
                elif w[0] in HYPHENS:
                    # Represent all hyphens the same way
                    yield TOK.Punctuation(HYPHEN)
                    w = w[1:]
                else:
                    yield TOK.Punctuation(w[0])
                    w = w[1:]
                if w == '"':
                    # We're left with a simple double quote: Convert to proper closing quote
                    w = '”'
            if w and '@' in w:
                # Check for valid e-mail
                # Note: we don't allow double quotes (simple or closing ones) in e-mails here
                # even though they're technically allowed according to the RFCs
                s = re.match(r"[^@\s]+@[^@\s]+(\.[^@\s\.,/:;\"”]+)+", w)
                if s:
                    ate = True
                    yield TOK.Email(s.group())
                    w = w[s.end():]
            # Numbers or other stuff starting with a digit
            if w and w[0] in DIGITS:
                ate = True
                t, eaten = parse_digits(w)
                yield t
                # Continue where the digits parser left off
                w = w[eaten:]
            # Alphabetic characters
            if w and w[0].isalpha():
                ate = True
                i = 1
                lw = len(w)
                while i < lw and (w[i].isalpha() or (w[i] in PUNCT_INSIDE_WORD and (i+1 == lw or w[i+1].isalpha()))):
                    # We allow dots to occur inside words in the case of
                    # abbreviations; also apostrophes are allowed within words and at the end
                    # (O'Malley, Mary's, it's, childrens', O‘Donnell)
                    i += 1
                # Make a special check for the occasional erroneous source text case where sentences
                # run together over a period without a space: 'sjávarútvegi.Það'
                a = w.split('.')
                if len(a) == 2 and a[0] and a[0][0].islower() and a[1] and a[1][0].isupper():
                    # We have a lowercase word immediately followed by a period and an uppercase word
                    yield TOK.Word(a[0], None)
                    yield TOK.Punctuation('.')
                    yield TOK.Word(a[1], None)
                    w = None
                else:
                    while w[i-1] == '.':
                        # Don't eat periods at the end of words
                        i -= 1
                    yield TOK.Word(w[0:i], None)
                    w = w[i:]
                    if w and w[0] in COMPOSITE_HYPHENS:
                        # This is a hyphen or en dash directly appended to a word:
                        # might be a continuation ('fjármála- og efnahagsráðuneyti')
                        # Yield a special hyphen as a marker
                        yield TOK.Punctuation(COMPOSITE_HYPHEN)
                        w = w[1:]
            if not ate:
                # Ensure that we eat everything, even unknown stuff
                yield TOK.Unknown(w[0])
                w = w[1:]
            # We have eaten something from the front of the raw token.
            # Check whether we're left with a simple double quote,
            # in which case we convert it to a proper closing double quote
            if w and w[0] == '"':
                w = '”' + w[1:]


def parse_particles(token_stream):
    """ Parse a stream of tokens looking for 'particles'
        (simple token pairs and abbreviations) and making substitutions """

    token = None
    try:

        # Maintain a one-token lookahead
        token = next(token_stream)
        while True:
            next_token = next(token_stream)
            # Make the lookahead checks we're interested in

            clock = False

            # Check for $[number]
            if token.kind == TOK.PUNCTUATION and token.txt == '$' and \
                next_token.kind == TOK.NUMBER:

                token = TOK.Amount(token.txt + next_token.txt, "USD", next_token.val[0]) # Unknown gender
                next_token = next(token_stream)

            # Check for €[number]
            if token.kind == TOK.PUNCTUATION and token.txt == '€' and \
                next_token.kind == TOK.NUMBER:

                token = TOK.Amount(token.txt + next_token.txt, "EUR", next_token.val[0]) # Unknown gender
                next_token = next(token_stream)

            # Coalesce abbreviations ending with a period into a single
            # abbreviation token
            if next_token.kind == TOK.PUNCTUATION and next_token.txt == '.':
                if token.kind == TOK.WORD and token.txt[-1] != '.' and ('.' in token.txt or
                    token.txt.lower() in Abbreviations.SINGLES or token.txt in Abbreviations.SINGLES):
                    # Abbreviation: make a special token for it
                    # and advance the input stream

                    # Check whether the following token is uppercase
                    # (and not a month name misspelled in upper case).
                    # If so, and this abbreviation is a common sentence finisher,
                    # yield it as well as an extra period
                    follow_token = next(token_stream)
                    finish = follow_token.kind == TOK.WORD and follow_token.txt[0].isupper() and \
                        not follow_token.txt.lower() in MONTHS and \
                        (token.txt + ".") in Abbreviations.FINISHERS

                    clock = token.txt.lower() == CLOCK_ABBREV

                    if finish:
                        # Yield the abbreviation and then the period token
                        token = TOK.Word("[" + token.txt + "]", None)
                        yield token
                        token = next_token
                    else:
                        token = TOK.Word("[" + token.txt + ".]", None)

                    next_token = follow_token

            # Coalesce 'klukkan'/[kl.] + time or number into a time
            if next_token.kind == TOK.TIME or next_token.kind == TOK.NUMBER:
                if clock or (token.kind == TOK.WORD and token.txt.lower() == CLOCK_WORD):
                    # Match: coalesce and step to next token
                    if next_token.kind == TOK.NUMBER:
                        token = TOK.Time(CLOCK_ABBREV + ". " + next_token.txt, next_token.val[0], 0, 0)
                    else:
                        token = TOK.Time(CLOCK_ABBREV + ". " + next_token.txt,
                            next_token.val[0], next_token.val[1], next_token.val[2])
                    next_token = next(token_stream)

            # Coalesce percentages into a single token
            if next_token.kind == TOK.PUNCTUATION and next_token.txt == '%':
                if token.kind == TOK.NUMBER:
                    # Percentage: convert to a percentage token
                    # In this case, there are no cases and no gender
                    token = TOK.Percent(token.txt + '%', token.val[0])
                    next_token = next(token_stream)

            # Coalesce ordinals (1. = first, 2. = second...) into a single token
            if next_token.kind == TOK.PUNCTUATION and next_token.txt == '.':
                if token.kind == TOK.NUMBER and not ('.' in token.txt or ',' in token.txt):
                    # Ordinal, i.e. whole number followed by period: convert to an ordinal token
                    follow_token = next(token_stream)
                    if follow_token.kind in (TOK.S_END, TOK.P_END) or \
                        (follow_token.kind == TOK.PUNCTUATION and follow_token.txt in {'„', '"'}) or \
                        (follow_token.kind == TOK.WORD and follow_token.txt[0].isupper() and
                        follow_token.txt.lower() not in MONTHS):
                        # Next token is a sentence or paragraph end,
                        # or opening quotes,
                        # or an uppercase word (and not a month name misspelled in upper case):
                        # fall back from assuming that this is an ordinal
                        yield token # Yield the number
                        token = next_token # The period
                        next_token = follow_token # The following (uppercase) word or sentence end
                    else:
                        # OK: replace the number and the period with an ordinal token
                        token = TOK.Ordinal(token.txt + '.', token.val[0])
                        # Continue with the following word
                        next_token = follow_token

            # Yield the current token and advance to the lookahead
            yield token
            token = next_token

    except StopIteration:
        # Final token (previous lookahead)
        if token:
            yield token


def parse_sentences(token_stream):
    """ Parse a stream of tokens looking for sentences, i.e. substreams within
        blocks delimited by sentence finishers (periods, question marks,
        exclamation marks, etc.) """

    in_sentence = False
    token = None
    try:

        # Maintain a one-token lookahead
        token = next(token_stream)
        while True:
            next_token = next(token_stream)

            if token.kind == TOK.P_BEGIN or token.kind == TOK.P_END:
                # Block start or end: finish the current sentence, if any
                if in_sentence:
                    yield TOK.End_Sentence()
                    in_sentence = False
                if token.kind == TOK.P_BEGIN and next_token.kind == TOK.P_END:
                    # P_BEGIN immediately followed by P_END:
                    # skip both and continue
                    token = None # Make sure we have correct status if next() raises StopIteration
                    token = next(token_stream)
                    continue
            else:
                if not in_sentence:
                    # This token starts a new sentence
                    yield TOK.Begin_Sentence()
                    in_sentence = True
                if token.kind == TOK.PUNCTUATION and token.txt in END_OF_SENTENCE:
                    # We may be finishing a sentence with not only a period but also
                    # right parenthesis and quotation marks
                    while next_token.kind == TOK.PUNCTUATION and next_token.txt in SENTENCE_FINISHERS:
                        yield token
                        token = next_token
                        next_token = next(token_stream)
                    # The sentence is definitely finished now
                    yield token
                    token = TOK.End_Sentence()
                    in_sentence = False

            yield token
            token = next_token

    except StopIteration:
        pass

    # Final token (previous lookahead)
    if token is not None:
        if not in_sentence and token.kind != TOK.P_END and token.kind != TOK.S_END:
            # Starting something here
            yield TOK.Begin_Sentence()
            in_sentence = True
        yield token
        if in_sentence and (token.kind == TOK.P_END or token.kind == TOK.S_END):
            in_sentence = False

    # Done with the input stream
    # If still inside a sentence, finish it
    if in_sentence:
        yield TOK.End_Sentence()


def annotate(token_stream, auto_uppercase):
    """ Look up word forms in the BIN word database. If auto_uppercase
        is True, change lower case words to uppercase if it looks likely
        that they should be uppercase. """

    at_sentence_start = False

    with closing(BIN_Db.get_db()) as db:
        # Consume the iterable source in wlist (which may be a generator)
        for t in token_stream:
            if t.kind != TOK.WORD:
                # Not a word: relay the token unchanged
                yield t
                if t.kind == TOK.S_BEGIN or (t.kind == TOK.PUNCTUATION and t.txt == ':'):
                    at_sentence_start = True
                elif t.kind != TOK.PUNCTUATION and t.kind != TOK.ORDINAL:
                    at_sentence_start = False
                continue
            if t.val is not None:
                # Already have a meaning
                yield t
                at_sentence_start = False
                continue
            # Look up word in BIN database
            w, m = db.lookup_word(t.txt, at_sentence_start, auto_uppercase)
            # Yield a word tuple with meanings
            yield TOK.Word(w, m)
            # No longer at sentence start
            at_sentence_start = False


# Recognize words that multiply numbers
MULTIPLIERS = {
    #"núll": 0,
    #"hálfur": 0.5,
    #"helmingur": 0.5,
    #"þriðjungur": 1.0 / 3,
    #"fjórðungur": 1.0 / 4,
    #"fimmtungur": 1.0 / 5,
    "einn": 1,
    "tveir": 2,
    "þrír": 3,
    "fjórir": 4,
    "fimm": 5,
    "sex": 6,
    "sjö": 7,
    "átta": 8,
    "níu": 9,
    "tíu": 10,
    "ellefu": 11,
    "tólf": 12,
    "þrettán": 13,
    "fjórtán": 14,
    "fimmtán": 15,
    "sextán": 16,
    "sautján": 17,
    "seytján": 17,
    "átján": 18,
    "nítján": 19,
    "tuttugu": 20,
    "þrjátíu": 30,
    "fjörutíu": 40,
    "fimmtíu": 50,
    "sextíu": 60,
    "sjötíu": 70,
    "áttatíu": 80,
    "níutíu": 90,
    #"par": 2,
    #"tugur": 10,
    #"tylft": 12,
    "hundrað": 100,
    "þúsund": 1000, # !!! Bæði hk og kvk!
    "þús.": 1000,
    "milljón": 1e6,
    "milla": 1e6,
    "milljarður": 1e9,
    "miljarður": 1e9,
    "ma.": 1e9
}

# Recognize words for fractions
FRACTIONS = {
    "þriðji": 1.0 / 3,
    "fjórði": 1.0 / 4,
    "fimmti": 1.0 / 5,
    "sjötti": 1.0 / 6,
    "sjöundi": 1.0 / 7,
    "áttundi": 1.0 / 8,
    "níundi": 1.0 / 9,
    "tíundi": 1.0 / 10,
    "tuttugasti": 1.0 / 20,
    "hundraðasti": 1.0 / 100,
    "þúsundasti": 1.0 / 1000,
    "milljónasti": 1.0 / 1e6
}

# Recognize words for percentages
PERCENTAGES = {
    "prósent": 1,
    "prósenta": 1,
    "hundraðshluti": 1,
    "prósentustig": 1
}

# Recognize words for nationalities (used for currencies)
NATIONALITIES = {
    "danskur": "dk",
    "enskur": "uk",
    "breskur": "uk",
    "bandarískur": "us",
    "kanadískur": "ca",
    "svissneskur": "ch",
    "sænskur": "se",
    "norskur": "no",
    "japanskur": "jp",
    "íslenskur": "is",
    "pólskur": "po",
    "kínverskur": "cn",
    "ástralskur": "au",
    "rússneskur": "ru",
    "indverskur": "in",
    "indónesískur": "id"
}

# Recognize words for currencies
CURRENCIES = {
    "króna": "ISK",
    "ISK": "ISK",
    "[kr.]": "ISK",
    "kr.": "ISK",
    "kr": "ISK",
    "pund": "GBP",
    "sterlingspund": "GBP",
    "GBP": "GBP",
    "dollari": "USD",
    "dalur": "USD",
    "bandaríkjadalur": "USD",
    "USD": "USD",
    "franki": "CHF",
    "rúbla": "RUB",
    "RUB": "RUB",
    "rúpía": "INR",
    "INR": "INR",
    "IDR": "IDR",
    "CHF": "CHF",
    "jen": "JPY",
    "yen": "JPY",
    "JPY": "JPY",
    "zloty": "PLN",
    "PLN": "PLN",
    "júan": "CNY",
    "yuan": "CNY",
    "CNY": "CNY",
    "evra": "EUR",
    "EUR": "EUR"
}

# Valid currency combinations
ISO_CURRENCIES = {
    ("dk", "ISK"): "DKK",
    ("is", "ISK"): "ISK",
    ("no", "ISK"): "NOK",
    ("se", "ISK"): "SEK",
    ("uk", "GBP"): "GBP",
    ("us", "USD"): "USD",
    ("ca", "USD"): "CAD",
    ("au", "USD"): "AUD",
    ("ch", "CHF"): "CHF",
    ("jp", "JPY"): "JPY",
    ("po", "PLN"): "PLN",
    ("ru", "RUB"): "RUB",
    ("in", "INR"): "INR", # Indian rupee
    ("id", "INR"): "IDR", # Indonesian rupiah
    ("cn", "CNY"): "CNY"
}

# Amount abbreviations including 'kr' for the ISK
# Corresponding abbreviations are found in Abbrev.conf
AMOUNT_ABBREV = {
    "þ.kr.": 1e3,
    "þús.kr.": 1e3,
    "m.kr.": 1e6,
    "mkr.": 1e6,
    "ma.kr.": 1e9
}

# Number words can be marked as subjects (any gender) or as numbers
NUMBER_CATEGORIES = frozenset(["töl", "to", "kk", "kvk", "hk", "lo"])


def match_stem_list(token, stems, filter_func=None):
    """ Find the stem of a word token in given dict, or return None if not found """
    if token.kind != TOK.WORD:
        return None
    # Go through the meanings with their stems
    if token.val:
        for m in token.val:
            # If a filter function is given, pass candidates to it
            try:
                lower_stofn = m.stofn.lower()
                if lower_stofn in stems and (filter_func is None or filter_func(m)):
                    return stems[lower_stofn]
            except Exception as e:
                print("Exception {0} in match_stem_list\nToken: {1}\nStems: {2}".format(e, token, stems))
                raise e
    # No meanings found: this might be a foreign or unknown word
    # However, if it is still in the stems list we return True
    return stems.get(token.txt.lower(), None)


def case(bin_spec, default="nf"):
    """ Return the case specified in the bin_spec string """
    c = default
    if "NF" in bin_spec:
        c = "nf"
    elif "ÞF" in bin_spec:
        c = "þf"
    elif "ÞGF" in bin_spec:
        c = "þgf"
    elif "EF" in bin_spec:
        c = "ef"
    return c


def add_cases(cases, bin_spec, default="nf"):
    """ Add the case specified in the bin_spec string, if any, to the cases set """
    c = case(bin_spec, default)
    if c:
        cases.add(c)


def all_cases(token):
    """ Return a list of all cases that the token can be in """
    cases = set()
    if token.kind == TOK.WORD:
        # Roll through the potential meanings and extract the cases therefrom
        if token.val:
            for m in token.val:
                if m.fl == "ob":
                    # One of the meanings is an undeclined word: all cases apply
                    cases = ALL_CASES
                    break
                add_cases(cases, m.beyging, None)
    return list(cases)


_GENDER_SET = { "kk", "kvk", "hk" }
_GENDER_DICT = { "KK": "kk", "KVK": "kvk", "HK": "hk" }

def all_genders(token):
    """ Return a list of the possible genders of the word in the token, if any """
    if token.kind != TOK.WORD:
        return None
    g = set()
    if token.val:
        for meaning in token.val:

            def find_gender(m):
                if m.ordfl in _GENDER_SET:
                    return m.ordfl # Plain noun
                # Probably number word ('töl' or 'to'): look at its spec
                for k, v in _GENDER_DICT.items():
                    if k in m.beyging:
                        return v
                return None

            gn = find_gender(meaning)
            if gn is not None:
               g.add(gn)
    return list(g)


def parse_phrases_1(token_stream):

    """ Parse a stream of tokens looking for phrases and making substitutions.
        First pass
    """

    token = None
    try:

        # Maintain a one-token lookahead
        token = next(token_stream)
        while True:
            next_token = next(token_stream)

            # Logic for numbers and fractions that are partially or entirely
            # written out in words

            def number(tok):
                """ If the token denotes a number, return that number - or None """
                if tok.txt.lower() == "áttu":
                    # Do not accept 'áttu' (stem='átta', no kvk) as a number
                    return None
                return match_stem_list(tok, MULTIPLIERS,
                    filter_func = lambda m: m.ordfl in NUMBER_CATEGORIES)

            def fraction(tok):
                """ If the token denotes a fraction, return a corresponding number - or None """
                return match_stem_list(tok, FRACTIONS)

            # Check whether we have an initial number word
            multiplier = number(token) if token.kind == TOK.WORD else None

            # Check for [number] 'hundred|thousand|million|billion'
            while (token.kind == TOK.NUMBER or multiplier is not None) \
                and next_token.kind == TOK.WORD:

                multiplier_next = number(next_token)

                def convert_to_num(token):
                    if multiplier is not None:
                        token = TOK.Number(token.txt, multiplier,
                            all_cases(token), all_genders(token))
                    return token

                if multiplier_next is not None:
                    # Retain the case of the last multiplier
                    token = convert_to_num(token)
                    token = TOK.Number(token.txt + " " + next_token.txt,
                        token.val[0] * multiplier_next,
                        all_cases(next_token), all_genders(next_token))
                    # Eat the multiplier token
                    next_token = next(token_stream)
                elif next_token.txt in AMOUNT_ABBREV:
                    # Abbreviations for ISK amounts
                    # For abbreviations, we do not know the case,
                    # but we try to retain the previous case information if any
                    token = convert_to_num(token)
                    token = TOK.Amount(token.txt + " " + next_token.txt, "ISK",
                        token.val[0] * AMOUNT_ABBREV[next_token.txt], # Number
                        token.val[1], token.val[2]) # Cases and gender
                    next_token = next(token_stream)
                else:
                    # Check for [number] 'percent'
                    percentage = match_stem_list(next_token, PERCENTAGES)
                    if percentage is not None:
                        token = convert_to_num(token)
                        token = TOK.Percent(token.txt + " " + next_token.txt, token.val[0],
                            all_cases(next_token), all_genders(next_token))
                        # Eat the percentage token
                        next_token = next(token_stream)
                    else:
                        break

                multiplier = None

            # Check for [number | ordinal] [month name]
            if (token.kind == TOK.ORDINAL or token.kind == TOK.NUMBER) and next_token.kind == TOK.WORD:

                month = match_stem_list(next_token, MONTHS)
                if month is not None:
                    token = TOK.Date(token.txt + " " + next_token.txt, y = 0, m = month,
                        d = token.val if token.kind == TOK.ORDINAL else token.val[0])
                    # Eat the month name token
                    next_token = next(token_stream)

            # Check for [date] [year]
            if token.kind == TOK.DATE and next_token.kind == TOK.YEAR:

                if not token.val[0]:
                    # No year yet: add it
                    token = TOK.Date(token.txt + " " + next_token.txt,
                        y = next_token.val, m = token.val[1], d = token.val[2])
                    # Eat the year token
                    next_token = next(token_stream)

            # Check for [date] [time]
            if token.kind == TOK.DATE and next_token.kind == TOK.TIME:

                # Create a time stamp
                y, mo, d = token.val
                h, m, s = next_token.val
                token = TOK.Timestamp(token.txt + " " + next_token.txt,
                    y = y, mo = mo, d = d, h = h, m = m, s = s)
                # Eat the time token
                next_token = next(token_stream)

            # Check for currency name doublets, for example
            # 'danish krona' or 'british pound'
            if token.kind == TOK.WORD and next_token.kind == TOK.WORD:
                nat = match_stem_list(token, NATIONALITIES)
                if nat is not None:
                    cur = match_stem_list(next_token, CURRENCIES)
                    if cur is not None:
                        if (nat, cur) in ISO_CURRENCIES:
                            # Match: accumulate the possible cases
                            token = TOK.Currency(token.txt + " "  + next_token.txt,
                                ISO_CURRENCIES[(nat, cur)], all_cases(token),
                                all_genders(next_token))
                            next_token = next(token_stream)

            # Check for composites:
            # 'stjórnskipunar- og eftirlitsnefnd'
            # 'viðskipta- og iðnaðarráðherra'
            # 'marg-ítrekaðri'
            if token.kind == TOK.WORD and \
                next_token.kind == TOK.PUNCTUATION and next_token.txt == COMPOSITE_HYPHEN:

                og_token = next(token_stream)
                if og_token.kind != TOK.WORD or (og_token.txt != "og" and og_token.txt != "eða"):
                    # Incorrect prediction: make amends and continue
                    if og_token.kind == TOK.WORD and token.txt in ADJECTIVE_PREFIXES:
                        # hálf-opinberri, marg-ítrekaðri
                        token = TOK.Word(token.txt + "-" + og_token.txt,
                            [m for m in og_token.val if m.ordfl == "lo" or m.ordfl == "ao"])
                        next_token = next(token_stream)
                    else:
                        yield token
                        # Put a normal hyphen instead of the composite one
                        token = TOK.Punctuation(HYPHEN)
                        next_token = og_token
                else:
                    # We have 'viðskipta- og'
                    final_token = next(token_stream)
                    if final_token.kind != TOK.WORD:
                        # Incorrect: unwind
                        yield token
                        yield TOK.Punctuation(HYPHEN) # Normal hyphen
                        token = og_token
                        next_token = final_token
                    else:
                        # We have 'viðskipta- og iðnaðarráðherra'
                        # Return a single token with the meanings of
                        # the last word, but an amalgamated token text.
                        # Note: there is no meaning check for the first
                        # part of the composition, so it can be an unknown word.
                        txt = token.txt + "- " + og_token.txt + \
                            " " + final_token.txt
                        token = TOK.Word(txt, final_token.val)
                        next_token = next(token_stream)

            # Yield the current token and advance to the lookahead
            yield token
            token = next_token

    except StopIteration:
        pass

    # Final token (previous lookahead)
    if token:
        yield token


def parse_phrases_2(token_stream):

    """ Parse a stream of tokens looking for phrases and making substitutions.
        Second pass
    """

    token = None
    try:

        # Maintain a one-token lookahead
        token = next(token_stream)

        # Maintain a set of full person names encountered
        names = set()

        at_sentence_start = False

        while True:
            next_token = next(token_stream)
            # Make the lookahead checks we're interested in

            # Check for [number] [currency] and convert to [amount]
            if token.kind == TOK.NUMBER and (next_token.kind == TOK.WORD or
                next_token.kind == TOK.CURRENCY):

                # Preserve the case of the number, if available
                # (milljónir, milljóna, milljónum)
                cases = token.val[1]
                genders = token.val[2]

                if next_token.kind == TOK.WORD:
                    # Try to find a currency name
                    cur = match_stem_list(next_token, CURRENCIES)
                    if cur is not None:
                        # Use the case and gender information from the currency name
                        if not cases:
                            cases = all_cases(next_token)
                        if not genders:
                            genders = all_genders(next_token)
                else:
                    # Already have an ISO identifier for a currency
                    cur = next_token.val[0]

                if cur is not None:
                    # Create an amount
                    # Use the case and gender information from the number, if any
                    token = TOK.Amount(token.txt + " " + next_token.txt,
                        cur, token.val[0], cases, genders)
                    # Eat the currency token
                    next_token = next(token_stream)

            # Logic for human names

            def stems(tok, categories, given_name = False):
                """ If the token denotes a given name, return its possible
                    interpretations, as a list of PersonName tuples (name, case, gender).
                    If first_name is True, we omit from the list all name forms that
                    occur in the disallowed_names section in the configuration file. """
                if tok.kind != TOK.WORD or not tok.val:
                    return None
                if at_sentence_start and tok.txt in NOT_NAME_AT_SENTENCE_START:
                    # Disallow certain person names at the start of sentences,
                    # such as 'Annar'
                    return None
                # Set up the names we're not going to allow
                dstems = DisallowedNames.STEMS if given_name else { }
                # Look through the token meanings
                result = []
                for m in tok.val:
                    if m.fl in categories:
                        # If this is a given name, we cut out name forms
                        # that are frequently ambiguous and wrong, i.e. "Frá" as accusative
                        # of the name "Frár", and "Sigurð" in the nominative.
                        c = case(m.beyging)
                        if m.stofn not in dstems or c not in dstems[m.stofn]:
                            # Note the stem ('stofn') and the gender from the word type ('ordfl')
                            result.append(PersonName(name = m.stofn, gender = m.ordfl, case = c))
                #if len(result) > 1:
                #    print("stems: result is\n{0}".format("\n".join(str(x) for x in result)))
                return result if result else None

            def has_category(tok, categories):
                """ Return True if the token matches a meaning with any of the given categories """
                if tok.kind != TOK.WORD or not tok.val:
                    return False
                return any(m.fl in categories for m in tok.val)

            def has_other_meaning(tok, category):
                """ Return True if the token can denote something besides a given name """
                if tok.kind != TOK.WORD or not tok.val:
                    return True
                # Look through the token meanings
                for m in tok.val:
                    if m.fl != category:
                        # Here is a different meaning, not a given name: return True
                        return True
                return False

            # Check for person names
            def given_names(tok):
                """ Check for Icelandic person name (category 'ism') """
                if tok.kind != TOK.WORD or not tok.txt[0].isupper():
                    # Must be a word starting with an uppercase character
                    return None
                return stems(tok, {"ism"}, given_name = True)

            # Check for surnames
            def surnames(tok):
                """ Check for Icelandic patronym (category 'föð') or matronym (category 'móð') """
                if tok.kind != TOK.WORD or not tok.txt[0].isupper():
                    # Must be a word starting with an uppercase character
                    return None
                return stems(tok, {"föð", "móð"})

            # Check for unknown surnames
            def unknown_surname(tok):
                """ Check for unknown (non-Icelandic) surnames """
                # Accept (most) upper case words as a surnames
                if tok.kind != TOK.WORD:
                    return False
                if not tok.txt[0].isupper():
                    # Must start with capital letter
                    return False
                if has_category(tok, {"föð", "móð"}):
                    # This is a known surname, not an unknown one
                    return False
                # Allow single-letter abbreviations, but not multi-letter
                # all-caps words (those are probably acronyms)
                return len(tok.txt) == 1 or not tok.txt.isupper()

            def given_names_or_middle_abbrev(tok):
                """ Check for given name or middle abbreviation """
                gnames = given_names(tok)
                if gnames is not None:
                    return gnames
                if tok.kind != TOK.WORD:
                    return None
                wrd = tok.txt
                if wrd.startswith('['):
                    # Abbreviation: Cut off the brackets & trailing period, if present
                    if wrd.endswith('.]'):
                        wrd = wrd[1:-2]
                    else:
                        # This is probably a C. which had its period cut off as a sentence ending...
                        wrd = wrd[1:-1]
                if len(wrd) > 2 or not wrd[0].isupper():
                    if wrd not in { "van", "de", "den", "der", "el", "al" }: # "of" was here
                        # Accept "Thomas de Broglie", "Ruud van Nistelroy"
                        return None
                # One or two letters, capitalized: accept as middle name abbrev,
                # all genders and cases possible
                return [PersonName(name = wrd, gender = None, case = None)]

            def compatible(pn, npn):
                """ Return True if the next PersonName (np) is compatible with the one we have (p) """
                if npn.gender and (npn.gender != pn.gender):
                    return False
                if npn.case and (npn.case != pn.case):
                    return False
                return True

            if token.kind == TOK.WORD and token.val and token.val[0].fl == "nafn":
                # Convert a WORD with fl="nafn" to a PERSON with the correct gender, in all cases
                gender = token.val[0].ordfl
                token = TOK.Person(token.txt, [ PersonName(token.txt, gender, case) for case in ALL_CASES ])
                gn = None
            else:
                gn = given_names(token)

            if gn:
                # Found at least one given name: look for a sequence of given names
                # having compatible genders and cases
                w = token.txt
                patronym = False
                while True:
                    ngn = given_names_or_middle_abbrev(next_token)
                    if not ngn:
                        break
                    # Look through the stuff we got and see what is compatible
                    r = []
                    for p in gn:
                        # noinspection PyTypeChecker
                        for np in ngn:
                            if compatible(p, np):
                                # Compatible: add to result
                                r.append(PersonName(name = p.name + " " + np.name, gender = p.gender, case = p.case))
                    if not r:
                        # This next name is not compatible with what we already
                        # have: break
                        break
                    # Success: switch to new given name list
                    gn = r
                    w += " " + (ngn[0].name if next_token.txt[0] == '[' else next_token.txt)
                    next_token = next(token_stream)

                # Check whether the sequence of given names is followed
                # by one or more surnames (patronym/matronym) of the same gender,
                # for instance 'Dagur Bergþóruson Eggertsson'

                def eat_surnames(gn, w, patronym, next_token):
                    """ Process contiguous known surnames, typically "*dóttir/*son", while they are
                        compatible with the given name we already have """
                    while True:
                        sn = surnames(next_token)
                        if not sn:
                            break
                        r = []
                        # Found surname: append it to the accumulated name, if compatible
                        for p in gn:
                            for np in sn:
                                if compatible(p, np):
                                    r.append(PersonName(name = p.name + " " + np.name, gender = p.gender, case = p.case))
                        if not r:
                            break
                        # Compatible: include it and advance to the next token
                        gn = r
                        w += " " + next_token.txt
                        patronym = True
                        next_token = next(token_stream)
                    return gn, w, patronym, next_token

                gn, w, patronym, next_token = eat_surnames(gn, w, patronym, next_token)

                # Must have at least one possible name
                assert len(gn) >= 1

                if not patronym:
                    # We stop name parsing after we find one or more Icelandic
                    # patronyms/matronyms. Otherwise, check whether we have an
                    # unknown uppercase word next;
                    # if so, add it to the person names we've already found
                    while unknown_surname(next_token):
                        for ix, p in enumerate(gn):
                            gn[ix] = PersonName(name = p.name + " " + next_token.txt, gender = p.gender, case = p.case)
                        w += " " + next_token.txt
                        next_token = next(token_stream)
                        # Assume we now have a patronym
                        patronym = True

                    if patronym:
                        # We still might have surnames coming up: eat them too, if present
                        gn, w, _, next_token = eat_surnames(gn, w, patronym, next_token)

                found_name = False
                # If we have a full name with patronym, store it
                if patronym:
                    names |= set(gn)
                else:
                    # Look through earlier full names and see whether this one matches
                    for ix, p in enumerate(gn):
                        gnames = p.name.split(' ') # Given names
                        for lp in names:
                            match = (not p.gender) or (p.gender == lp.gender)
                            if match:
                                # The gender matches
                                lnames = set(lp.name.split(' ')[0:-1]) # Leave the patronym off
                                for n in gnames:
                                    if n not in lnames:
                                        # We have a given name that does not match the person
                                        match = False
                                        break
                            if match:
                                # All given names match: assign the previously seen full name
                                gn[ix] = PersonName(name = lp.name, gender = lp.gender, case = p.case)
                                found_name = True
                                break

                # If this is not a "strong" name, backtrack from recognizing it.
                # A "weak" name is (1) at the start of a sentence; (2) only one
                # word; (3) that word has a meaning that is not a name;
                # (4) the name has not been seen in a full form before.

                weak = at_sentence_start and (' ' not in w) and not patronym and \
                    not found_name and has_other_meaning(token, "ism")

                if not weak:
                    # Return a person token with the accumulated name
                    # and the intersected set of possible cases
                    token = TOK.Person(w, gn)

            # Yield the current token and advance to the lookahead
            yield token

            if token.kind == TOK.S_BEGIN or (token.kind == TOK.PUNCTUATION and token.txt == ':'):
                at_sentence_start = True
            elif token.kind != TOK.PUNCTUATION and token.kind != TOK.ORDINAL:
                at_sentence_start = False
            token = next_token

    except StopIteration:
        pass

    # Final token (previous lookahead)
    if token:
        yield token


def parse_static_phrases(token_stream, auto_uppercase):

    """ Parse a stream of tokens looking for static multiword phrases
        (i.e. phrases that are not affected by inflection).
        The algorithm implements N-token lookahead where N is the
        length of the longest phrase.
    """

    tq = [] # Token queue
    state = defaultdict(list) # Phrases we're considering
    pdict = StaticPhrases.DICT # The phrase dictionary

    try:

        while True:

            token = next(token_stream)

            if token.txt is None: # token.kind != TOK.WORD:
                # Not a word: no match; discard state
                if tq:
                    for t in tq: yield t
                    tq = []
                if state:
                    state = defaultdict(list)
                yield token
                continue

            # Look for matches in the current state and build a new state
            newstate = defaultdict(list)
            wo = token.txt # Original word
            w = wo.lower() # Lower case
            if wo == w:
                wo = w

            def add_to_state(slist, index):
                """ Add the list of subsequent words to the new parser state """
                wrd = slist[0]
                rest = slist[1:]
                newstate[wrd].append((rest, index))

            # First check for original (uppercase) word in the state, if any;
            # if that doesn't match, check the lower case
            wm = None
            if wo is not w and wo in state:
                wm = wo
            elif w in state:
                wm = w

            if wm:
                # This matches an expected token:
                # go through potential continuations
                tq.append(token) # Add to lookahead token queue
                token = None
                for sl, ix in state[wm]:
                    if not sl:
                        # No subsequent word: this is a complete match
                        # Reconstruct original text behind phrase
                        w = " ".join([t.txt for t in tq])
                        # Add the entire phrase as one 'word' to the token queue
                        yield TOK.Word(w, [BIN_Meaning._make(r) for r in StaticPhrases.get_meaning(ix)])
                        # Discard the state and start afresh
                        newstate = defaultdict(list)
                        w = wo = ""
                        tq = []
                        # Note that it is possible to match even longer phrases
                        # by including a starting phrase in its entirety in
                        # the static phrase dictionary
                        break
                    add_to_state(sl, ix)
            elif tq:
                for t in tq: yield t
                tq = []

            wm = None
            if auto_uppercase and len(wo) == 1 and w is wo:
                # If we are auto-uppercasing, leave single-letter lowercase
                # phrases alone, i.e. 'g' for 'gram' and 'm' for 'meter'
                pass
            elif wo is not w and wo in pdict:
                wm = wo
            elif w in pdict:
                wm = w

            # Add all possible new states for phrases that could be starting
            if wm:
                # This word potentially starts a phrase
                for sl, ix in pdict[wm]:
                    if not sl:
                        # Simple replace of a single word
                        if tq:
                            for t in tq: yield tq
                            tq = []
                        # Yield the replacement token
                        yield TOK.Word(token.txt, [BIN_Meaning._make(r) for r in StaticPhrases.get_meaning(ix)])
                        newstate = defaultdict(list)
                        token = None
                        break
                    add_to_state(sl, ix)
                if token:
                    tq.append(token)
            elif token:
                yield token

            # Transition to the new state
            state = newstate

    except StopIteration:
        # Token stream is exhausted
        pass

    # Yield any tokens remaining in queue
    for t in tq: yield t


def disambiguate_phrases(token_stream):

    """ Parse a stream of tokens looking for common ambiguous multiword phrases
        (i.e. phrases that have a well known very likely interpretation but
        other extremely uncommon ones are also grammatically correct).
        The algorithm implements N-token lookahead where N is the
        length of the longest phrase.
    """

    tq = [] # Token queue
    state = defaultdict(list) # Phrases we're considering
    pdict = AmbigPhrases.DICT # The phrase dictionary

    try:

        while True:

            token = next(token_stream)

            if token.kind != TOK.WORD:
                # Not a word: no match; yield the token queue
                if tq:
                    for t in tq: yield t
                    tq = []
                # Discard the previous state, if any
                if state:
                    state = defaultdict(list)
                # ...and yield the non-matching token
                yield token
                continue

            # Look for matches in the current state and build a new state
            newstate = defaultdict(list)
            w = token.txt.lower()

            def add_to_state(slist, index):
                """ Add the list of subsequent words to the new parser state """
                wrd = slist[0]
                rest = slist[1:]
                newstate[wrd].append((rest, index))

            if w in state:
                # This matches an expected token:
                # go through potential continuations
                tq.append(token) # Add to lookahead token queue
                token = None
                for sl, ix in state[w]:
                    if not sl:
                        # No subsequent word: this is a complete match
                        # Discard meanings of words in the token queue that are not
                        # compatible with the category list specified
                        cats = AmbigPhrases.get_cats(ix)
                        # assert len(cats) == len(tq)
                        # print("Matching ambiguous phrase")
                        for t, cat in zip(tq, cats):
                            # assert t.kind == TOK.WORD
                            # Yield a new token with fewer meanings for each original token in the queue
                            #print("Ambig word {0}:\n   original meanings {1}\n   restricted meanings {2}"
                            #    .format(t.txt, t.val, [m for m in t.val if m.ordfl == cat]))
                            yield TOK.Word(t.txt, [m for m in t.val if m.ordfl == cat])

                        # Discard the state and start afresh
                        if newstate:
                            newstate = defaultdict(list)
                        w = ""
                        tq = []
                        # Note that it is possible to match even longer phrases
                        # by including a starting phrase in its entirety in
                        # the static phrase dictionary
                        break
                    add_to_state(sl, ix)
            elif tq:
                # This does not continue a started phrase:
                # yield the accumulated token queue
                for t in tq: yield t
                tq = []

            if w in pdict:
                # This word potentially starts a new phrase
                for sl, ix in pdict[w]:
                    # assert sl
                    add_to_state(sl, ix)
                if token:
                    tq.append(token) # Start a lookahead queue with this token
            elif token:
                # Not starting a new phrase: pass the token through
                yield token

            # Transition to the new state
            state = newstate

    except StopIteration:
        # Token stream is exhausted
        pass

    # Yield any tokens remaining in queue
    for t in tq: yield t


def recognize_entities(token_stream, enclosing_session = None):

    """ Parse a stream of tokens looking for (capitalized) entity names
        The algorithm implements N-token lookahead where N is the
        length of the longest entity name having a particular initial word.
    """

    tq = [] # Token queue
    state = defaultdict(list) # Phrases we're considering
    ecache = dict() # Entitiy definition cache
    lastnames = dict() # Last name to full name mapping ('Clinton' -> 'Hillary Clinton')

    with SessionContext(session = enclosing_session, commit = True) as session:

        def fetch_entities(w, fuzzy = True):
            """ Return a list of entities matching the word(s) given,
                exactly if fuzzy = False, otherwise also as a starting word(s) """
            q = session.query(Entity.name, Entity.verb, Entity.definition)
            if fuzzy:
                q = q.filter(Entity.name.like(w + " %") | (Entity.name == w))
            else:
                q = q.filter(Entity.name == w)
            return q.all()

        def query_entities(w):
            """ Return a list of entities matching the initial word given """
            e = ecache.get(w)
            if e is None:
                ecache[w] = e = fetch_entities(w)
            return e

        def flush_match():
            """ Flush a match that has been accumulated in the token queue """
            if len(tq) == 1 and tq[0].txt in lastnames:
                # If single token, it may be the last name of a
                # previously seen entity or person
                return token_or_entity(tq[0])
            # Reconstruct original text behind phrase
            ename = " ".join([t.txt for t in tq])
            # We don't include the definitions in the token - they should be looked up
            # on the fly when processing or displaying the parsed article
            return TOK.Entity(ename, None)

        def token_or_entity(token):
            """ Return a token as-is or, if it is a last name of a person that has already
                been mentioned in the token stream by full name, refer to the full name """
            assert token.txt[0].isupper()
            if token.txt not in lastnames:
                # Not a last name of a previously seen full name
                return token
            tfull = lastnames[token.txt]
            if tfull.kind != TOK.PERSON:
                # Return an entity token with no definitions
                # (this will eventually need to be looked up by full name when
                # displaying or processing the article)
                return TOK.Entity(token.txt, None)
            # Return the full name meanings
            return TOK.Person(token.txt, tfull.val)

        try:

            while True:

                token = next(token_stream)

                if not token.txt: # token.kind != TOK.WORD:
                    if state:
                        if None in state:
                            yield flush_match()
                        else:
                            for t in tq:
                                yield t
                        tq = []
                        state = defaultdict(list)
                    yield token
                    continue

                # Look for matches in the current state and build a new state
                newstate = defaultdict(list)
                w = token.txt # Original word

                def add_to_state(slist, entity):
                    """ Add the list of subsequent words to the new parser state """
                    wrd = slist[0] if slist else None
                    rest = slist[1:]
                    newstate[wrd].append((rest, entity))

                if w in state:
                    # This matches an expected token
                    tq.append(token) # Add to lookahead token queue
                    # Add the matching tails to the new state
                    for sl, entity in state[w]:
                        add_to_state(sl, entity)
                    # Update the lastnames mapping
                    fullname = " ".join([t.txt for t in tq])
                    parts = fullname.split()
                    # If we now have 'Hillary Rodham Clinton',
                    # make sure we delete the previous 'Rodham' entry
                    for p in parts[1:-1]:
                        if p in lastnames:
                            del lastnames[p]
                    if parts[-1][0].isupper():
                        # 'Clinton' -> 'Hillary Rodham Clinton'
                        lastnames[parts[-1]] = TOK.Entity(fullname, None)
                else:
                    # Not a match for an expected token
                    if state:
                        if None in state:
                            # Flush the already accumulated match
                            yield flush_match()
                        else:
                            for t in tq:
                                yield t
                        tq = []

                    # Add all possible new states for entity names that could be starting
                    weak = True
                    cnt = 1
                    upper = w and w[0].isupper()
                    parts = None

                    if upper and " " in w:
                        # For all uppercase phrases (words, entities, persons),
                        # maintain a map of last names to full names
                        parts = w.split()
                        lastname = parts[-1]
                        # Clinton -> Hillary [Rodham] Clinton
                        if lastname[0].isupper():
                            # Look for Icelandic patronyms/matronyms
                            _, m = BIN_Db.get_db().lookup_word(lastname, False)
                            if m and any(mm.fl in { "föð", "móð" } for mm in m):
                                # We don't store Icelandic patronyms/matronyms as surnames
                                pass
                            else:
                                lastnames[lastname] = token

                    if token.kind == TOK.WORD and upper:
                        if " " in w:
                            # w may be a person name with more than one embedded word
                            # parts is assigned in the if statement above
                            cnt = len(parts)
                        elif not token.val:
                            # No BÍN meaning for this token
                            weak = False # Accept single-word entity references
                        # elist is a list of Entity instances
                        elist = query_entities(w)
                    else:
                        elist = []

                    if elist:
                        # This word might be a candidate to start an entity reference
                        candidate = False
                        for e in elist:
                            sl = e.name.split()[cnt:] # List of subsequent words in entity name
                            if sl:
                                # Here's a candidate for a longer entity reference than we already have
                                candidate = True
                            if sl or not weak:
                                add_to_state(sl, e)
                        if weak and not candidate:
                            # Found no potential entity reference longer than this token
                            # already is - and we have a BÍN meaning for it: Abandon the effort
                            assert not newstate
                            assert not tq
                            yield token_or_entity(token)
                        else:
                            # Go for it: Initialize the token queue
                            tq = [ token ]
                    else:
                        # Not a start of an entity reference: simply yield the token
                        assert not tq
                        if upper:
                            # Might be a last name referring to a full name
                            yield token_or_entity(token)
                        else:
                            yield token

                # Transition to the new state
                state = newstate

        except StopIteration:
            # Token stream is exhausted
            pass

        # Yield an accumulated match if present
        if state:
            if None in state:
                yield flush_match()
            else:
                for t in tq:
                    yield t
            tq = []

    # print("\nEntity cache:\n{0}".format("\n".join("'{0}': {1}".format(k, v) for k, v in ecache.items())))
    # print("\nLast names:\n{0}".format("\n".join("{0}: {1}".format(k, v) for k, v in lastnames.items())))

    assert not tq


def tokenize(text, auto_uppercase = False, enclosing_session = None):
    """ Tokenize text in several phases, returning a generator (iterable sequence) of tokens
        that processes tokens on-demand. If auto_uppercase is True, the tokenizer
        attempts to correct lowercase words that probably should be uppercase. """

    # Thank you Python for enabling this programming pattern ;-)

    token_stream = parse_tokens(text)

    token_stream = parse_particles(token_stream)

    token_stream = parse_sentences(token_stream)

    token_stream = parse_static_phrases(token_stream, auto_uppercase) # Static multiword phrases

    token_stream = annotate(token_stream, auto_uppercase) # Lookup meanings from dictionary

    token_stream = parse_phrases_1(token_stream) # First phrase pass

    token_stream = parse_phrases_2(token_stream) # Second phrase pass

    token_stream = recognize_entities(token_stream, enclosing_session) # Recognize named entities from database

    token_stream = disambiguate_phrases(token_stream) # Eliminate very uncommon meanings

    return token_stream


def paragraphs(toklist):
    """ Generator yielding paragraphs from a token list. Each paragraph is a list
        of sentence tuples. Sentence tuples consist of the index of the first token
        of the sentence (the TOK.S_BEGIN token) and a list of the tokens within the
        sentence, not including the terminating TOK.S_END token. """
    if not toklist:
        return
    sent = [] # Current sentence
    sent_begin = 0
    current_p = [] # Current paragraph
    for ix, t in enumerate(toklist):
        t0 = t[0]
        if t0 == TOK.S_BEGIN:
            sent = []
            sent_begin = ix
        elif t0 == TOK.S_END:
            if sent:
                # Do not include or count zero-length sentences
                current_p.append((sent_begin, sent))
                sent = []
        elif t0 == TOK.P_BEGIN or t0 == TOK.P_END:
            # New paragraph marker: Start a new paragraph if we didn't have one before
            # or if we already had one with some content
            if sent:
                current_p.append((sent_begin, sent))
                sent = []
            if current_p:
                yield current_p
                current_p = []
        else:
            sent.append(t)
    if sent:
        current_p.append((sent_begin, sent))
    if current_p:
        yield current_p

