# deletes all files from all buckets
# remove all objects from bucket
mc rm --recursive --force default/aced-default
mc rm --recursive --force default/aced-public
mc rm --recursive --force ohsu/aced-ohsu
mc rm --recursive --force ucl/aced-ucl
mc rm --recursive --force manchester/aced-manchester
mc rm --recursive --force stanford/aced-stanford
