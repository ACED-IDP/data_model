Coherent Synthetic Data Set
===========================

This data set contains 1,278 [Fast Healthcare Interoperabiliy Resources (FHIR)](https://www.hl7.org/fhir) [Bundles](https://www.hl7.org/fhir/bundle.html) in JavaScript Object Notation (JSON) format in the `fhir` directory. Each of these files contains a simulated longitudinal clinical record. These records contain references to simulated medical imaging, DNA testing.


Getting started:
-------

* Setup venv
```sh

python3 -m venv venv 
source venv/bin/activate

```

* Install dependencies

```sh
pip install -r requirements.txt
```

* Transform coherent data

```sh

# see scripts/coherent_etl.sh
```

* Load into gen3

```sh

# see scripts/gen3_etl.sh
```


License
-------
This Coherent Synthetic Data Set is made available under the Creative Commons Attribution 4.0 International License: https://creativecommons.org/licenses/by/4.0/

Copyright 2021 - The MITRE Corporation

MITRE Public Release Case Number: 21-1917