

## coherent dataset

* Get the coherent dataset

```commandline
wget http://hdx.mitre.org/downloads/coherent-08-10-2021.zip
unzip coherent-08-10-2021.zip

```


* Setup this repo

```commandline
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install numpy cython
pip install -r requirements.txt
pip install -r requirements-dev.txt
cp -r output coherent
```

* Transform the data

```commandline
python3 scripts/transform.py
```
  * Expected output
    ```commandline
    60473 - __mp_main__ - INFO - Parsed coherent/output/fhir/Janean397_Bradtke547_c4b8f200-66af-4de6-47ae-c98df28998b1.json in 0.6010 seconds, wrote output/Janean397_Bradtke547_c4b8f200-66af-4de6-47ae-c98df28998b1.json
    ....    
    60467 - __main__ - INFO - Parsed all files in coherent/output/fhir in 109.9279 seconds

* Test the data

```commandline
pytest tests/integration/
```
