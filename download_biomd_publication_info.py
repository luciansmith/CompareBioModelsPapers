# -*- coding: utf-8 -*-
"""
Created on Wed Jun 12 16:14:37 2024

@author: Lucian
"""

import os
import json
import requests
import uuid
from pprint import pprint

root_biomodels = "https://www.ebi.ac.uk/biomodels/"
all_publications = {}

for i in range(1, 1104):
# for i in skipped:
    if i in [649, 694, 992, 993, 1049, 1050, 1051]: #Don't exist
        continue
    print(i)
    # if i in [1066, 1067, 1068, 1069, 1070, 1071, 1073, 1074, 1075, 1076, ]: #Aren't SBML]
    #     continue
    num = f'{i:010d}'
    biomd = "BIOMD" + num

    oldmd = requests.get(root_biomodels + biomd, params={"format": "json"})
    oldmetadata = oldmd.json()
    if oldmetadata['publication']['link'] not in all_publications:
        all_publications[oldmetadata['publication']['link']] = oldmetadata['publication']
        all_publications[oldmetadata['publication']['link']]['BioModel(s)'] = []
    all_publications[oldmetadata['publication']['link']]['BioModel(s)'].append(biomd)

pprint(all_publications)
with open('biomd_publication_info.json', 'w', encoding='utf-8') as f:
    json.dump(all_publications, f, ensure_ascii=False, indent=4)
