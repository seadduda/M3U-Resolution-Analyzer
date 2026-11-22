# M3U Resolution Analyzer

Lokalni dashboard koji učita M3U listu (fajl ili URL), provjeri svaki kanal pomoću `ffprobe`/`ffmpeg` i prikaže koliko kanala ima koju rezoluciju i kodek.

## Pokretanje

Dvoklik na `pokreni_analyzer.bat` ili:

```
python analyzer_server.py
```

Dashboard: http://127.0.0.1:8767 (ne smeta Control Roomu na 8765).

Potrebni su `ffprobe` i `ffmpeg` (`winget install Gyan.FFmpeg.Essentials`); server ih sam nalazi u PATH-u ili u WinGet folderu.

## Šta radi

- Učitavanje `.m3u/.m3u8/.txt` fajla ili URL-a (Xtream `get.php` itd.).
- Za svaki kanal: kodek (HEVC/H.264...), rezolucija, FPS, **vrijeme koje je trebalo da se izvuče rezolucija (ms)** i **Mbps** (izmjereno kratkim uzorkom stream-a, `uzorak (s)`, ili iz ffprobe bit_rate ako je mjerenje isključeno).
- Dashboard u jednom redu: ukupno, provjereno, radi, ne radi te kartice **HEVC** i **H.264** s brojem kanala i razlomkom po 4K / FHD / HD / SD. Mbps i vrijeme se vide samo u tablici kanala.
- Kategorije: 4K (visina ≥ 2160), FHD (≥ 1080), HD (≥ 720), SD (ostalo).
- Klik na karticu filtrira listu kanala; kolone se sortiraju klikom na naslov.
- Kanali s drugim kodekom (npr. MPEG-2) ili bez videa se ne uključuju u H.265/H.264 izvoz.
- **Izvoz M3U**: dva kvadrata (H.265 i H.264), svaki s redovima 4K, FHD, HD, SD i dugmetom Izvezi, ili **ZIP sa svim odvojeno** (`H265/FHD_H265.m3u`, `H264/HD_H264.m3u`, `H265/H265_SVE.m3u`, ...). Opcionalno se u naziv kanala doda oznaka npr. `[1920x1080 HEVC 4.2 Mbps]`.

## Savjeti

- Filmovi/serije (`/movie/`, `/series/`) se po defaultu preskaču; isključi opciju ako ih želiš provjeriti.
- Mnogi provajderi ograničavaju broj istovremenih konekcija: ako dobiješ puno grešaka, smanji **Paralelno**.
- Mjerenje Mbps drži svaki kanal otvoren još ~5 s; isključi ga za brzu provjeru samo rezolucije.
- **Ponovi neuspjele** ponovo provjerava kanale koji su pali (timeout, preopterećenje).
