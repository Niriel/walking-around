# How to walk around my neighborhood

## Set up the stack

### Install the packages
```bash
sudo apt update
sudo apt install -y \
    qgis qgis-plugin-grass \
    gdal-bin \
    postgresql postgis \
    python3-venv python3-pip \
    build-essential
```

### Check installation
```bash
gdalinfo --version
ogr2ogr --version
```

## Create the database

```bash
sudo systemctl enable --now postgresql
sudo -u postgres createuser -s "$USER"
createdb gis
psql gis -c "CREATE EXTENSION postgis;"
psql gis -c "SELECT PostGIS_Version();"
```

It should print
```
            postgis_version            
---------------------------------------
 3.6 USE_GEOS=1 USE_PROJ=1 USE_STATS=1
(1 row)
```

## Create Python project

### Install old python so that Fiona doesn't break

In the project root:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

uv venv --python 3.13
source .venv/bin/activate
uv pip install rasterio geopandas shapely numpy pillow requests pyproj pyogrio
```
