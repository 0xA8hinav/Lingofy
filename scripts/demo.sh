#!/bin/bash
set -e
python3 -c "from scripts.demo import run_demo; import sys; r=run_demo(sys.argv[1] if len(sys.argv)>1 else 'tests/fixtures/tone_16s.wav', source='en', target='hi'); print(r)"
