cd /home/mdsuzon/Documents/Land
source .venv/bin/activate

python convorter.py

python land_portal.py --dev                   # while developing (devtools block OFF)
python land_portal.py                         # devtools block ON
BD_ADMIN_KEY=secret python land_portal.py     # admin API needs this key