import json
import Bio.Entrez
Bio.Entrez.email = 'lpsmith@uw.edu'


all_publications = {}

with open('biomd_publication_info_2.json', 'r', encoding='utf-8') as f:
    all_publications = json.load(f)

other_pubs = {}
weights = {}
for publication in all_publications:
    pubinfo = all_publications[publication]
    if "Similar PMIDs" in pubinfo:
        weight = len(all_publications[publication]["Similar PMIDs"])
        if weight not in weights:
            weights[weight] = 0
        weights[weight] += 1
        # for pmid in all_publications[publication]["Similar PMIDs"]:

print(weights)