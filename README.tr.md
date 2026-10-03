# Terrain Matters: tekrar üretim paketi

[English](README.md) · **Türkçe**

Bu paket şu makalenin kodunu, verisini ve çıktılarını içerir:

> **Terrain Matters: Does Planar Routing Mislead Energy-Optimal Design of Electric Fleets? Evidence from a Hilly Town**
> Ahmet Ertuğrul Arık ve M. Ali Ülkü. *Logistics* (MDPI) için hazırlanan makale.

> [!NOTE]
> **Durum.** Makale hazırlanıyor. `data/parameters/vehicle_profiles.json` içindeki araç parametreleri `DOĞRULANACAK`
> etiketli yer tutuculardır. Kaynakları eklenip koşular yinelendiğinde bütün mutlak sayılar değişecek. Arşivlenen
> sürümün DOI'si buraya eklenecek.

<p align="center">
  <img src="docs/paper/figures/fig01_terrain_network.png" alt="Copernicus GLO-30 yükseklik modeli üzerinde Ürgüp yol ağı ve durakları" width="640">
</p>

## Soru

Filo rotaları düzlemsel yol ağlarında tasarlanır. Bu ağlar yolların nereye gittiğini bilir, ne kadar tırmandığını
bilmez. Elektrikli araçta bu önemlidir: rejeneratif frenleme inişin enerjisinin yalnızca bir kısmını, yalnızca bir güç
sınırına kadar geri kazanır, hafif inişlerde ise hiç kazanmaz. Çalışma düzlemsel modelin iki açıdan ne kadar yanıldığını
soruyor:

- **tahmin olarak:** verilen bir rotanın düzlemsel tahmin hatası;
- **karar olarak:** *arazi bilgisinin değeri* (VoTI). VoTI, düzlemsel optimum ağın gerçek arazide, aynı duraklar ve
  aynı araç için arazi optimum ağa göre fazladan harcadığı enerjidir.

Vaka Ürgüp'ün dolmuş ağı: 8 hat, 1.948 yoldan oluşan yönlü bir yol grafiği ve 976–1.599 m arasında rölyef. Deneyler rejen
verimini, araç kütlesini, rölyef yoğunluğunu ve yükseklik gürültüsünü tarıyor.

## İçerik

| Yol | İçerik |
|---|---|
| `urgup_transport/energy.py` | Kesim enerji modeli: yuvarlanma, aerodinamik, eğim ve yardımcı yük; rejen verimi ve güç sınırıyla. Salt standart kütüphane |
| `urgup_transport/elevation.py` | Bir geometri boyunca griddeki yüksekliği okur, geometriye yazmaz |
| `urgup_transport/optimizer.py` | Mesafe, süre ve enerji matrisleri (potansiyel düzeltmeli Dijkstra), OR-Tools rotalama, rota gerçekleştirme |
| `urgup_transport/…`, `webapp/optimization_service.py` | Optimizer'ın içe aktardığı veri katmanı ve yardımcı modüller |
| `scripts/run_terrain_experiments.py` | Ana koşu: A–E ve R deneyleri |
| `scripts/run_r1_revision_experiments.py` | E0–E7 revizyon deneyleri; ayrıca `report` ve `figures` |
| `scripts/plot_terrain_figures.py` | Koşu CSV'lerinden Şekil 1–9 |
| `scripts/build_elevation.py` | Copernicus GLO-30'u indirip yükseklik gridini kurar |
| `tests/` | Enerji modelinin dört kapalı tur özdeşliği ve Bellman–Ford'a karşı enerji rotalaması |
| `data/editable/network.json` | Girdi: durak kaydı, mahalleler ve yayınlanan hatlar (taslak revizyonu 135) |
| `data/editable/road_network.geojson` | Girdi: yönlü yol grafiği (revizyon 1.584) |
| `data/processed/baseline/baseline_network.json` | Girdi: bugün işletilen 8 hat, dondurulmuş |
| `data/parameters/vehicle_profiles.json` | Araç parametreleri (yer tutucu) |
| `data/processed/experiments/2026-09-29-urgup/` | Ana koşunun çıktıları |
| `data/processed/experiments/2026-10-R1/` | Revizyon koşularının çıktıları |
| `docs/paper/figures/`, `docs/paper/revision_R1/` | Bu çıktılardan çizilen şekiller ve tablolar; R1 raporu |
| `SOURCE.json`, `SHA256SUMS` | Dışa aktarımın kaynağı ve her dosyanın sağlama toplamı |

Kod, yazarların Ürgüp Belediyesi için geliştirdiği planlama platformundan dışa aktarıldı; platform public değil. Pakette
yalnızca deneylerin ihtiyaç duyduğu kısım var ve klasör adları kodun beklediği adlardır.

## Kurulum

Python 3.12 ya da üstü gerekir; paket 3.12.3 ve 3.14.4 üzerinde sınandı. Yayınlanan koşular Linux üzerinde
Python 3.14.4 ve OR-Tools 9.15.6755 ile yapıldı.

```bash
git clone https://github.com/elestirmen/terrain-matters.git
cd terrain-matters
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`DATABASE_URL` tanımlı **olmamalı**. Tanımlı değilse kod girdilerini `data/editable/` altından okur.

## Enerji modelini sınama

```bash
python -m unittest tests.test_energy tests.test_energy_routing
```

Testler saniyeler sürer. Modelin dört kapalı tur özdeşliğini sınarlar:

1. Düz arazide DEM ve düzlem enerjisi eşittir.
2. Kayıpsız bir araç kapalı turda enerji harcamaz.
3. Bir halkanın iki yönü yalnızca kayıp yokken eşittir.
4. Simetrik bir tepe (1/η<sub>drive</sub> − η<sub>regen</sub>)·m·g·H'ye mal olur.

Ayrıca potansiyel düzeltmeli en kısa yolların ham enerjiler üzerindeki Bellman–Ford sonucuna eşit olduğunu sınarlar.

## Yükseklik gridini kurma

```bash
python scripts/build_elevation.py
```

Betik kenti kapsayan iki Copernicus GLO-30 paftasını indirir ve `data/processed/elevation/glo30-v1/` klasörünü yazar.
Koşu manifestleri kullandıkları gridin sha256 değerini kaydeder (`manifest.json` içinde `elevation.sha256`). Yeniden
kurulan grid bu değerle eşleşmelidir.

## Deneyleri yeniden koşma

**Ana koşu.** 57 optimizasyondan oluşur; her biri 15 s, Monte Carlo tekrarları 5 s. Yayınlanan koşunun devamı gibi
okunmaması için yeni bir koşu kimliği verin:

```bash
python scripts/run_terrain_experiments.py --run-id benim-kosum --experiments A,B,C,D,E,R
python scripts/plot_terrain_figures.py --run-id benim-kosum --out benim-sekillerim
```

`data/processed/experiments/benim-kosum/network_metrics.csv` dosyasını yayınlanan
`2026-09-29-urgup/network_metrics.csv` ile karşılaştırın. Yayından önce A deneyi (16 optimizasyon) bu paketin temiz
bir kurulumundan Python 3.14 ile yeniden koşuldu ve 4 çekirdekli bir Intel N100'de 5 dk 21 s sürdü. 36 ağ satırının ve
576 hat satırının hepsi, her koşuda yeniden üretilen hat kimlikleri dışında, yayınlanan koşuyla birebir aynı çıktı.

**Revizyon deneyleri.** Toplamda yaklaşık 3,5 saat çözücü süresi tutar; yalnızca E1 yaklaşık 1,7 saattir. Betik kayıtlı
hücrelerden devam eder; yeniden okumak yerine hesaplamak için yayınlanan klasörü kenara alın:

```bash
mv data/processed/experiments/2026-10-R1 data/processed/experiments/2026-10-R1.published
python scripts/run_r1_revision_experiments.py E1      # E1 … E7, birer birer
python scripts/run_r1_revision_experiments.py report  # docs/paper/revision_R1/REPORT.md
python scripts/run_r1_revision_experiments.py figures
```

E7 AW3D30 paftalarını Microsoft Planetary Computer'dan indirir.

### Tekrar üretilebilirlik notları

- **Girdiler.** Girdiler yayınlanan koşunun çözüldüğü girdilerin aynısıdır. Taslak revizyonu koşu manifestiyle, yol ağı
  her önerinin kaydettiği özetle karşılaştırıldı. Sonuç `SOURCE.json` içinde.
- **Çözücü.** OR-Tools bu örnekte yinelenen koşularda özdeş ağlar verdi. 15 s ve 60 s sınırları da aynı ağı verdi,
  çünkü guided local search 15 s'den çok önce duruyor. Bu yüzden makine hızının etkisi küçük olmalı, ama farklı bir
  OR-Tools sürümü arama yolunu değiştirebilir.
- **Rastgelelik.** Monte Carlo ve düğüm sırası tohumları betiklerde sabit ve her manifestte kayıtlı.
- **Manifestler.** Her koşunun `manifest.json` dosyası platform commit'ini, DEM sağlama toplamını, araç profilini ve
  onun sağlama toplamını, bütün parametreleri kaydeder.
- **Optimum değil, bulunan en iyi.** Çözücü süre sınırlı bir sezgiseldir, bu yüzden bir çözüm "optimum" değil, "bulunan
  en iyi"dir.

## Veri ve lisanslar

| Kısım | Lisans |
|---|---|
| Kod | MIT ([`LICENSE`](LICENSE)) |
| Yol geometrisi (`data/editable/road_network.geojson`, her koşunun `routes.geojson` ve `proposals/*.json` dosyaları) | ODbL 1.0 ([`LICENSE-ODbL`](LICENSE-ODbL)), © OpenStreetMap katkıcıları |
| Diğer veri, tablolar ve şekiller | CC BY 4.0 ([`LICENSE-DATA`](LICENSE-DATA)) |

Yol grafiği OpenStreetMap'ten türetildi ve Ürgüp Belediyesi'nin yol orta hatlarıyla tamamlandı. Duraklar ve işletilen
hatlar belediyenin açık verisinden geliyor. Yükseklik gridleri yeniden dağıtılmaz. Kaynaklar ve zorunlu atıf metinleri
[`ATTRIBUTION.md`](ATTRIBUTION.md) dosyasında.

## Atıf

Lütfen makaleye atıf yapın; ayrıntılar [`CITATION.cff`](CITATION.cff) dosyasında.

```bibtex
@unpublished{arik_ulku_terrain_matters,
  author = {Ar{\i}k, Ahmet Ertu{\u{g}}rul and {\"U}lk{\"u}, M. Ali},
  title  = {Terrain Matters: Does Planar Routing Mislead Energy-Optimal Design of
            Electric Fleets? Evidence from a Hilly Town},
  note   = {Manuscript in preparation for \emph{Logistics} (MDPI)},
  year   = {2026}
}
```

## Yazarlar

| Yazar | Kurum | ORCID |
|---|---|---|
| **Ahmet Ertuğrul Arık** | Bilişim Sistemleri ve Teknolojileri Bölümü, Kapadokya Üniversitesi, Ürgüp, Nevşehir | [0000-0002-7952-4311](https://orcid.org/0000-0002-7952-4311) |
| **M. Ali Ülkü** (sorumlu yazar) | Department of Management Science and Information Systems ve Centre for Research in Sustainable Supply Chain Analytics (CRSSCA), Faculty of Management, Dalhousie University, Halifax, Kanada | [0000-0002-8495-3364](https://orcid.org/0000-0002-8495-3364) |

**Üretken yapay zekâ kullanımı.** Enerji modeli, deney betiği ve şekil betikleri yazarların tasarımı ve denetimi
altında Claude Code (Anthropic) ile yazıldı. Her denklemi, testi ve sonucu yazarlar doğruladı.

**Teşekkür.** Yol orta hatları, durak kaydı ve güzergâh kayıtları Ürgüp Belediyesi'nden alındı.
