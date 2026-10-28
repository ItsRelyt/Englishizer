@echo off
title Python Setup and Execution

echo Installing required Python packages to user space...
python -m pip install --user pynput pyspellchecker

echo.
echo Running writing_assistant.py...
python writing_assistant.py

echo.
echo Script finished.
pause
