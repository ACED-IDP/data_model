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

# count number of distinct fields we have per type
for file_name in output_dir.glob("*.ndjson"):
    entity_name = None
    with open(file_name) as f:
        for line in f.readlines():
            record = json.loads(line)
            entity_name = record['name']
            entity_keys = sorted(record['object'].keys())
            # entity_keys = [k for k in entity_keys if 'coding' not in k]
            entity_keys = [k for k in entity_keys if (not k.startswith('name') and not k.startswith('result'))]
            entity_keys = ','.join(entity_keys)
            if entity_keys not in models[entity_name]:
                models[entity_name][entity_keys] = 0
            models[entity_name][entity_keys] += 1


print("######")
print("Count distinct fieldsets per entity")
print("######")
for entity_name in sorted(models):
    print(entity_name, 'Count', len(models[entity_name]))

# print("######")
# print("Detail fieldsets per entity")
# print("######")
# for entity_name in sorted(models):
#     print(entity_name)
#     sorted_keys_by_frequency = dict(sorted(models[entity_name].items(), key=lambda item: item[1]))
#     for key_set in sorted_keys_by_frequency:
#         print('\t', models[entity_name][key_set], key_set)


print("######")
print("Consensus diff: compare \"consensus\" aka majority fieldset for entity against other variations")
print("######")
for entity_name in sorted(models):
    # sort keys by count ascending
    sorted_keys_by_frequency = dict(sorted(models[entity_name].items(), key=lambda item: item[1]))
    # discard those with only one set
    if len(sorted_keys_by_frequency) == 1:
        continue
    most_popular = list(sorted_keys_by_frequency.keys())[-1]
    most_popular_count = sorted_keys_by_frequency[most_popular]
    most_popular = set(most_popular.split(','))
    print('## ')
    print(entity_name, 'frequency', most_popular_count, 'consensus', sorted(most_popular))
    # in descending order
    for less_popular in reversed(list(sorted_keys_by_frequency.keys())[:-1]):
        frequency = sorted_keys_by_frequency[less_popular]
        less_popular = set(less_popular.split(','))
        print(f'\t frequency', frequency, 'symmetric_difference', most_popular.symmetric_difference(less_popular))
    # second_most_popular = set(list(sorted_keys_by_frequency.keys())[-2].split(','))
    # print('\t symmetric_difference', most_popular.symmetric_difference(second_most_popular))
    # print('\t\t fields in the second most popular not in most popular', second_most_popular - most_popular)
    # print('\t\t fields in the most popular not in second most popular', most_popular - second_most_popular)
