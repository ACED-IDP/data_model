

## coherent dataset

* Read the paper

> This is a synthetic data set that includes FHIR resources, DICOM images, genomic data, physiological data (i.e., ECGs), and simple clinical notes. FHIR links all the data types together.

> Walonoski J, Hall D, Bates KM, Farris MH, Dagher J, Downs ME, Sivek RT, Wellner B, Gregorowicz A, Hadley M, Campion FX, Levine L, Wacome K, Emmer G, Kemmer A, Malik M, Hughes J, Granger E, Russell S. The “Coherent Data Set”: Combining Patient Data and Imaging in a Comprehensive, Synthetic Health Record. Electronics. 2022; 11(8):1199. https://doi.org/10.3390/electronics11081199

see https://doi.org/10.3390/electronics11081199

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
# transform the coherent data (fix references, add missing resources)
python3 scripts/transform.py
# create the pfb
python3 scripts/pfb.py | sh
# create figures
./scripts/visualize.sh 

```

  * Expected output
    ```commandline
    60473 - __mp_main__ - INFO - Parsed coherent/output/fhir/Janean397_Bradtke547_c4b8f200-66af-4de6-47ae-c98df28998b1.json in 0.6010 seconds, wrote output/Janean397_Bradtke547_c4b8f200-66af-4de6-47ae-c98df28998b1.json
    ....    
    60467 - __main__ - INFO - Parsed all files in coherent/output/fhir in 109.9279 seconds
    ...
    copied png files to docs/
    ```
 
* Test the data
    
    ```commandline
    pytest tests/integration/
    ```

* See [data model](data_model.md)

