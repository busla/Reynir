import os
import codecs
import sys

cwd = os.path.dirname(__file__)

def run_txt_parser(file):
    """ Read input file and output CSV """
    out = codecs.open(file['outfile'], 'a', 'utf-8')
    with codecs.open(file['infile'], 'r', 'iso-8859-1') as inp:
        for li in inp:            
            if li:
                li = li.strip()
            if li:
                if not li.startswith('#'):
                    forms = li.split(u' ')
                    if forms:                        
                        word = forms[0]
                        for f in forms[1:]:
                            s = u'{0};{1};{2};{3};{4};{5}\n'.format(
                                word, 0, f, u'ob', word, u'-'
                            )                                                
                            out.write(s)

def run_csv_parser(file):

    #out = codecs.open(file['outfile'], 'a', file['out-encoding'])

    with codecs.open(file['infile'], 'r', file['in-encoding']) as f:
        lines = f.readlines()
        for i, line in enumerate(lines):
            if 'nafnháttarmerki' in line:
                print(line)
            

files = [
    {
        'infile': cwd + '../resources/ord.csv',
        'outfile': cwd+'../resources/ord.csv',
        'in-encoding': 'utf-8',
        'out-encoding': 'utf-8',
        'start-line': 0,
        'func': run_csv_parser
    }
]


if __name__ == '__main__':
    for file in files:
        if not os.path.isfile(file['infile']):            
            sys.exit('Neccessary file is missing: {}'.format(file['infile']))
        else:
            print('Retrieving data from file: {}'.format(file['infile']))
            file['func'](file)
            print('Data appended successfully to file: {}'.format(file['outfile']))
    print('And we are done :D')