@echo on
conda list -p "%PREFIX%" --canonical
if errorlevel 1 exit 1
conda list -p "%PREFIX%" --canonical | grep "conda-build-test-python-xx-1\.0-py35_0"
if errorlevel 1 exit 1
