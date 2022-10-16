docker inspect --type=image anonymizer > /dev/null
if [ $? -ne 0 ]; then
    docker build -t anonymizer .
fi
docker run --rm \
  -v $(pwd)/anonymizer/configuration.json:/Tools-for-Health-Data-Anonymization/configuration-sample.json \
  -v $(pwd)/output:/input \
  -v $(pwd)/output/anonymized:/anonymized \
  anonymizer cli -i /input -o /anonymized $@