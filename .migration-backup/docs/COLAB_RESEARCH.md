# Train separate research candidates in Google Colab

Upload `notebooks/Frozen_stock_research.ipynb` using Colab's **File → Upload notebook**. Choose a CPU runtime and run cells in order. Colab may currently start with a newer Python version; the notebook uses `uv` to provision a managed Python 3.12 virtual environment, then installs the project's pinned dependencies there. The host Python version is therefore not used for model fitting. The notebook checks out repository commit `1015c9bd2959f14fc0b7fb7bc9916ce22dfd646d` and embeds its small auditable worker, so it works without publishing new helper files to GitHub first.

## Private input

The supplied default manifest pin is the existing baseline model. The notebook requires its four-file ZIP, not raw unverified CSV, exchange credentials or a full workspace archive. Prepare it locally:

```sh
python backend/scripts/prepare_colab_input.py \
  --model .paper-training/history-ml/candidates/baseline/69bf19577ac014ba67cc81a960b6ff2ef200c2270a8cc58fbd1cbc394ca2d05a \
  --manifest-sha256 3b33ea952b92c0eaace7267cdb7d3164060c07f6d2615eb847760dd4080dc6d7 \
  --output .paper-training/colab/colab-input.zip
```

The bundle contains only manifest.json, dataset.csv, calibrated_model.json and final_test_predictions.csv. No database, account state, credentials or forward evidence is exported. Preparing the ZIP does not upload anything; choosing it in Colab sends those files to Google. Keep the bundle and notebook outputs private. Other year-long candidate artifacts may be used only with their independently verified manifest pin. Hashes establish integrity, not provider authenticity.

## Training and persistence

Default jobs train fixed random forests separately for baseline and stress costs. `FAMILY="auto"` instead uses the existing development-only selection between logistic regression and random forest. Original gap policy is preserved; missing candles are not fabricated. Each completed job exports a numeric model, exact input dataset bytes, diagnostics, walk-forward results, metadata and checksums. No registration, promotion or broker access occurs.

The notebook downloads each completed ZIP. Optional `SAVE_TO_DRIVE=True` asks for Google Drive authorization and copies completed exports to a new folder, verifying their checksums. Training happens on the VM, not directly on mounted Drive. A session interruption requires restarting an unfinished fit in a fresh workspace; completed exported profiles need not be repeated. The notebook does not claim mid-fit resume support.

The current sklearn algorithms use CPU. Colab GPU selection does not convert them to GPU implementations. The notebook does not launch persistent trading workers or try to bypass Colab's timeouts. Colab resource availability and session lifetime are limited: https://research.google.com/colaboratory/faq.html

Existing historical data has already been examined. These results are exploratory, not new forward evidence. Review any exported candidate and start a separately frozen future experiment before judging changes. The running 84-day experiment is not modified.
