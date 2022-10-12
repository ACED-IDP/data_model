"""Examine the patterns of data for each model's simplified keys."""
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict
from pprint import pprint


def recursive_default_dict() -> Dict:
    """Recursive default dict."""
    return defaultdict(recursive_default_dict)


output_dir = Path('DATA/pfb')

models = recursive_default_dict()

for file_name in output_dir.glob("*.ndjson"):
    entity_name = None
    with open(file_name) as f:
        for line in f.readlines():
            record = json.loads(line)
            entity_name = record['name']
            entity_keys = sorted(record['object'].keys())
            # entity_keys = [k for k in entity_keys if 'coding' not in k]
            entity_keys = ','.join(entity_keys)
            if entity_keys not in models[entity_name]:
                models[entity_name][entity_keys] = 0
            models[entity_name][entity_keys] += 1
print("######")
print("Distinct fieldsets per entity")
print("######")
for entity_name in sorted(models):
    print(entity_name, len(models[entity_name]))

print("######")
print("Detail fieldsets per entity")
print("######")
for entity_name in sorted(models):
    print(entity_name)
    sorted_keys_by_frequency = dict(sorted(models[entity_name].items(), key=lambda item: item[1]))
    for key_set in sorted_keys_by_frequency:
        print('\t', models[entity_name][key_set], key_set)


print("######")
print("Detail fieldsets per entity")
print("######")
for entity_name in sorted(models):
    print(entity_name)
    sorted_keys_by_frequency = dict(sorted(models[entity_name].items(), key=lambda item: item[1]))
    for key_set in sorted_keys_by_frequency:
        print('\t', models[entity_name][key_set], key_set)


print("######")
print("Diff - top two field sets for each entity")
print("######")
for entity_name in sorted(models):
    sorted_keys_by_frequency = dict(sorted(models[entity_name].items(), key=lambda item: item[1]))
    if len(sorted_keys_by_frequency) == 1:
        continue
    print(entity_name)
    most_popular = set(list(sorted_keys_by_frequency.keys())[-1].split(','))
    second_most_popular = set(list(sorted_keys_by_frequency.keys())[-2].split(','))
    print('\t symmetric_difference', most_popular.symmetric_difference(second_most_popular))
    print('\t\t fields in the second most popular not in most popular', second_most_popular - most_popular)
    print('\t\t fields in the most popular not in second most popular', most_popular - second_most_popular)
