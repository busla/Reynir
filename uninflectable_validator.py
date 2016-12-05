with open("obeyg.smaord.txt", encoding='ISO-8859-1') as file:   # Use file to refer to the file object
    data = file.readlines()
    for line in data:
        #print(len(line.split()))
        if not len(line.split()) == 2:
            print('Of löng lína: ', line)