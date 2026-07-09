# Maps Lead Finder

Posta kodu, adres veya sehir merkezli km yaricapinda firma arar. Varsayilan mod `gosom/google-maps-scraper` entegrasyonudur. API anahtari gerekmez; Docker veya yerel `google-maps-scraper` binary gerekir.

## Calistirma

```powershell
cd C:\Users\info\Desktop\TECDOC\maps_lead_finder
python app.py
```

Sonra tarayicida acin:

```text
http://127.0.0.1:8765
```

Bu oturumda yeni API'siz surum su portta acik:

```text
http://127.0.0.1:8767
```

## Google Scraper Modu

`config.json` icinde varsayilan kaynak:

```json
"source": "gosom"
```

Bu mod `gosom/google-maps-scraper` aracini calistirir. Docker varsa otomatik olarak su image kullanilir:

```text
gosom/google-maps-scraper
```

Docker kullanmak istemezseniz binary indirip `config.json` icinde yolunu verin:

```json
"gosom_use_docker": false,
"gosom_binary": "google_maps_scraper-1.16.1-windows-amd64.exe"
```

Relative yol yazarsaniz yol `maps_lead_finder` klasorune gore cozulur. Ornek: `tools\\google_maps_scraper.exe` yazarsaniz dosya `maps_lead_finder\\tools\\google_maps_scraper.exe` icinde aranir.

## OpenStreetMap Modu

Google yerine OpenStreetMap/Overpass kullanmak isterseniz:

```json
"source": "osm"
```

API anahtari gerekmez. Veri kalitesi Google Maps kadar dolu olmayabilir; telefon, web sitesi ve e-posta bilgileri sadece OpenStreetMap'te veya firmanin web sitesinde acik olarak varsa gelir.

## Config Alanlari

- `source`: `gosom`, `osm` veya `google`. API'siz Google scraping icin `gosom`.
- `default_location`: Posta kodu, sehir veya acik adres.
- `default_radius_km`: Arama yaricapi.
- `default_queries`: Her satir/oge ayri arama kriteridir.
- `fetch_emails_from_websites`: Web sitelerinde e-posta arama acik/kapali.
- `email_pages`: E-posta aramak icin kontrol edilecek site yollari.
- `overpass_limit`: OSM modunda cekilecek en fazla ham kayit sayisi.
- `overpass_timeout_seconds`: OSM Overpass sorgusu icin bekleme suresi. Zaman asimi olursa artirilabilir.
- `overpass_retry_delay_seconds`: Overpass 429 yogunluk hatasinda diger sunucuya gecmeden once beklenecek sure.
- `gosom_use_docker`: `gosom` modunda Docker image kullanilsin.
- `gosom_binary`: Docker yerine yerel binary yolu.
- `gosom_depth`: Google Maps scroll derinligi.
- `gosom_fast_mode`: Daha hizli temel sonuc modu.
- `gosom_exit_on_inactivity`: Scraper hareketsiz kalinca cikis suresi.

Zaman asimi veya 429 olursa once yaricapi 1-3 km yapin, e-posta aramayi kapali tutun ve daha net arama kriterleri kullanin. Ornek: `kfz werkstatt` yerine `reifenservice` veya `autoteile`.

## Google Modu Istege Bagli

Google Places kullanmak isterseniz:

```json
"source": "google",
"google_api_key": "GOOGLE_MAPS_API_KEYINIZ"
```

Google modunda Places API ve Geocoding API etkin olmalidir. API anahtari olmadan kullanmak icin `source` alanini `osm` birakin.
