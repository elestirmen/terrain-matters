<p align="center"><img src="assets/icon.png" alt="terrain-matters simgesi" width="120"></p>

<p align="center">
  <img src="docs/assets/terrain-matters-banner.jpg" alt="Çizim: düz bir sokak haritasından çıkıp bir Kapadokya kasabasının engebeli arazisine tırmanan dolmuş hatları" width="100%">
</p>

<h1 align="center">Terrain Matters · Tekrar Üretim Paketi</h1>

<p align="center">
  <strong>Gerçek arazide elektrikli filo için enerji odaklı rota tasarımı çalışmasının kodu, verisi ve çıktıları</strong>
</p>

<p align="center">
  <a href="README.md">English</a> · <b>Türkçe</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/OR--Tools-9.15.6755-4285F4?style=flat-square" alt="OR-Tools">
  <img src="https://img.shields.io/badge/DEM-Copernicus%20GLO--30-2E7D32?style=flat-square" alt="DEM">
  <img src="https://img.shields.io/badge/makale-hazırlanıyor-orange?style=flat-square" alt="Makale">
  <img src="https://img.shields.io/badge/kod-MIT-blue?style=flat-square" alt="Kod lisansı">
  <img src="https://img.shields.io/badge/veri-CC%20BY%204.0%20%7C%20ODbL%201.0-lightgrey?style=flat-square" alt="Veri lisansları">
</p>

---

Bu paket şu makalenin kodunu, verisini ve çıktılarını içerir:

> **Terrain Matters: Does Planar Routing Mislead Energy-Optimal Design of Electric Fleets? Evidence from a Hilly Town**
> Ahmet Ertuğrul Arık ve M. Ali Ülkü. *Logistics* (MDPI) için hazırlanan makale.

Çalışma, Ürgüp dolmuş ağının gerçek yol grafiği, durak kaydı ve işletilen hatları üzerinde koşar. Bu paketle okur
enerji modelini sınayabilir, yükseklik gridini yeniden kurabilir, her deneyi yeniden koşabilir ve makaledeki her şekil
ve tabloyu yeniden çizebilir.

> [!NOTE]
> **Durum (Ekim 2026).** Makale hazırlanıyor.
> [`data/parameters/vehicle_profiles.json`](data/parameters/vehicle_profiles.json) içindeki araç parametreleri
> `DOĞRULANACAK` etiketli yer tutuculardır. Bu yüzden aşağıdaki bulgular yön ve mekanizma olarak anlatılıyor.
> Parametrelerin kaynağı bulunup koşular yinelendiğinde bütün mutlak sayılar değişecek. Sayıların kendisi koşu
> çıktılarında ve R1 raporunda. Arşivlenen sürümün DOI'si buraya eklenecek.

---

## İçindekiler

**Araştırma**

- [Araştırma sorusu](#araştırma-sorusu)
- [Alt sorular ve hipotezler](#alt-sorular-ve-hipotezler)
- [Çalışma alanı ve veri](#çalışma-alanı-ve-veri)
- [Yöntem](#yöntem)
- [Metrikler](#metrikler)
- [Deneyler](#deneyler)
- [Ön bulgular](#ön-bulgular)
- [Kapsam ve sınırlılıklar](#kapsam-ve-sınırlılıklar)

**Tekrar üretim**

- [Paketin içeriği](#paketin-içeriği)
- [Kurulum](#kurulum)
- [Enerji modelini sınama](#enerji-modelini-sınama)
- [Yükseklik gridini kurma](#yükseklik-gridini-kurma)
- [Deneyleri yeniden koşma](#deneyleri-yeniden-koşma)
- [Çıktılar](#çıktılar)
- [Şekiller ve tablolar](#şekiller-ve-tablolar)
- [Tekrar üretilebilirlik notları](#tekrar-üretilebilirlik-notları)
- [Veri ve lisanslar](#veri-ve-lisanslar)
- [Atıf, yazarlar ve yapay zekâ kullanımı](#atıf-yazarlar-ve-yapay-zekâ-kullanımı)

---

# Araştırma

## Araştırma sorusu

> **Elektrikli bir filoda rota tasarımı 3B yüzey yerine düzlemsel yol ağı üzerinde yapılınca ne kadar enerji
> kaybedilir? Arazi bilgisi rota kararlarını gerçekten değiştirir mi?**

Filo rotaları neredeyse her zaman bir harita üzerinde tasarlanır: uzunluğu ve en iyi ihtimalle hızı bilinen yol
kesimlerinden oluşan bir grafik. Amaç mesafe, süre ya da bunlardan biriyle orantılı bir maliyettir. Bu *düzlemsel*
model yolların nereye gittiğini bilir, ne kadar tırmandığını bilmez. Dizel araçta bu eksik tolere edilebilir, çünkü
yokuşta harcanan yakıt inişte geri kazanılmaz. Elektrikli araçta durum farklıdır. Rejeneratif frenleme inişin
potansiyel enerjisinin bir kısmını geri kazanır ve bunun üç sınırı vardır:

- yalnızca bir payını geri kazanır (η<sub>regen</sub>);
- motorun ve bataryanın alabildiği güce kadar geri kazanır (P<sup>max</sup><sub>regen</sub>);
- iniş yuvarlanma direncini yenecek kadar dikse geri kazanır.

Bu yüzden kapalı bir turda bataryadan çekilen enerji mesafenin, hatta toplam tırmanışın fonksiyonu değildir. Tırmanış
ve inişlerin turun *neresinde* olduğuna, *hangi yönde* alındığına ve *ne kadar dik* olduğuna bağlıdır.

Çalışma iki tür hatayı birbirinden ayırır:

| | Muhasebe hatası | Karar hatası |
|---|---|---|
| Ne yanlış? | Verilen bir rotanın enerji değeri | Seçilen rotanın kendisi |
| Ölçüsü | Düzlemsel tahmin hatası, E<sub>düzlem</sub> / E<sub>DEM</sub> − 1 | Arazi bilgisinin değeri (**VoTI**) |
| Sonradan düzeltilebilir mi? | Evet, rota arazide yeniden değerlendirilir | Hayır, çünkü rotalar seçilmiş durumda |

**VoTI**, düzlemsel optimum ağın gerçek arazide fazladan harcadığı enerjidir. Karşılaştırma aynı duraklar, aynı araç ve
aynı hizmet için arazi optimum ağla yapılır:

```math
\mathrm{VoTI} = E_{DEM}(R_{\text{düzlem-opt}}) - E_{DEM}(R_{\text{DEM-opt}})
```

VoTI hem düzlemsel enerji optimum ağa hem mesafe optimum ağa karşı raporlanır, çünkü uygulamada kullanılan mesafe
optimum ağdır.

## Alt sorular ve hipotezler

| | Alt soru |
|---|---|
| RQ1 | Aynı rotaların düzlemsel ve 3B enerjisi arasındaki muhasebe hatası ne kadar büyük? |
| RQ2 | Amaç 3B enerji olunca hat üyelikleri ve ziyaret sıraları değişiyor mu, ne kadar? |
| RQ3 | Mesafede özdeş olan bir kapalı halkanın iki yönü enerjide ne kadar farklı? |
| RQ4 | Bu etkiler rejen verimi, araç kütlesi ve rölyef yoğunluğuyla nasıl ölçekleniyor? |
| RQ5 | 30 m'lik bir DEM'in düşey belirsizliğine ne kadar duyarlılar? |

| | Hipotez |
|---|---|
| **H1** | Düzlemsel model tur enerjisini sistematik olarak küçümser ve fark η<sub>regen</sub> düştükçe büyür. |
| **H2** | Kaybı toplam tırmanış değil eğim dağılımı belirler. Bu yüzden bir halkanın iki yönü mesafede özdeş, enerjide farklıdır; farkı sürtünme freni payı açıklar. |
| **H3** | Belirli bir arazi yoğunluğu eşiğinin üstünde arazi bilgisi hat üyeliğini değiştirir; eşiğin altında yalnızca muhasebeyi değiştirir. |

## Çalışma alanı ve veri

Ürgüp, Orta Anadolu'da Kapadokya bölgesinde yaklaşık 20.000 nüfuslu bir ilçe merkezidir. Mahalleleri küçük bir merkezin
çevresindeki yamaçlara ve sırtlara yayılır, bir hat da kent dışındaki Aksalur köyüne gider. Hizmet yolcu taşımacılığıdır.
Planlama problemi ise dağıtım lojistiğindeki depo merkezli, kapalı turlu araç rotalama problemidir. Kent test ortamı
olarak seçildi, çünkü gerçek yol geometrisini, gerçekten işletilen hatları ve güçlü rölyefi bir arada sunuyor. Bu
bileşim yayımlanmış kıyas veri setlerinde nadirdir.

| Kalem | Çalışmada kullanılan | Bu pakette |
|---|---|---|
| Hatlar | Tek merkez duraktan (depo) kalkan 8 belediye dolmuş hattı | |
| Duraklar | 130 duraklık planlama kaydı (129 müşteri durağı ve depo), taslak revizyonu 135 | [`data/editable/network.json`](data/editable/network.json) |
| Yol grafiği | 1.948 yol nesnesinden oluşan yönlü grafik (revizyon 1.584; 4.321 yönlü kenar). OpenStreetMap'ten türetildi; OSM'de olmayan ara sokaklar belediyenin MAKS yol orta hatlarından eklendi. Tek yön kuralları belediyenin trafik levhası kayıtlarından, yol sınıflarının bir kısmı Ulaşım Ana Planı'ndan geliyor | [`data/editable/road_network.geojson`](data/editable/road_network.geojson) |
| Hızlar | Belediye değeri ya da yol sınıfına göre varsayılan (50 / 45 / 40 / 30 / 20 / 15 km/sa). Bunlar yasal ya da varsayılan hızlardır, ölçülmüş işletme hızları değildir | Yol grafiğinin içinde |
| Yükseklik | Copernicus DEM GLO-30 (30 m aralık, LE90 4 m'nin altında); 976–1.599 m aralığını kapsayan 345 × 403 grid, veri seti `glo30-v1`, sağlama toplamıyla. Revizyon deneylerinde ikinci DEM olarak JAXA AW3D30 (`aw3d30-v1`) kullanılıyor | Yeniden dağıtılmaz; [`scripts/build_elevation.py`](scripts/build_elevation.py) kurar |
| Bugünkü hatlar | Belediyenin güzergâh çizimlerinden ve durak kaydından sayısallaştırılmış 8 hat; dondurulmuş, salt okunur referans katmanı | [`data/processed/baseline/baseline_network.json`](data/processed/baseline/baseline_network.json) |
| Araç | 14 koltuklu elektrikli minibüs sınıfı; parametreler yer tutucu (bkz. [Kapsam ve sınırlılıklar](#kapsam-ve-sınırlılıklar)) | [`data/parameters/vehicle_profiles.json`](data/parameters/vehicle_profiles.json) |

Duraklar grafa, en yakın yol durağın izdüşümünde bölünerek bağlanır. Herhangi bir yola 120 m'den uzak bir durak düz
çizgiyle bağlanmaz, reddedilir. Yönlü yolu olmayan bir durak çifti büyük daire mesafesiyle doldurulmaz, hata olarak
raporlanır (`road_graph_unreachable`).

<p align="center">
  <img src="docs/paper/figures/fig01_terrain_network.png" alt="Copernicus GLO-30 yükseklik modeli üzerinde Ürgüp yol ağı ve durakları" width="720">
  <br><sub><b>Makalenin Şekil 1'i.</b> Belediye yol ağı ve planlama kaydındaki duraklar, Copernicus GLO-30 üzerinde.</sub>
</p>

## Yöntem

Model [`urgup_transport/energy.py`](urgup_transport/energy.py) dosyasında ve yalnızca Python standart kütüphanesini
kullanıyor. En kısa yollar ve optimizer [`urgup_transport/optimizer.py`](urgup_transport/optimizer.py) içinde.

### Kesim enerji modeli

Her yönlü yol kesimi **25 m** adımla yeniden örneklenir ve her örnekte yükseklik gridden çift doğrusal
interpolasyonla okunur. İç örneklere **100 m**'lik hareketli ortalama uygulanır. İki uç örnek düğümün ham kotunu korur;
böylece kesimdeki yükselişlerin toplamı tam olarak uçtan uca yükselişe eşit olur. Yüzey modeli yar yüzlerini ve çatıları
okuduğu için eğim **|s| ≤ 0,20** ile sınırlanır. Alt kesim *k* üzerinde tekerdeki net kuvvet:

```math
F_k = m g C_{rr}\cos\theta_k + \tfrac{1}{2}\rho\, C_dA\, v^2 + m g \sin\theta_k
```

Bataryadan çekilen enerji:

```math
E_k = \begin{cases} P_k t_k / \eta_{drive}, & P_k \ge 0 \\ -\,\eta_{regen}\, \min(|P_k|, P^{max}_{regen})\, t_k, & P_k < 0 \end{cases}
```

Burada $P_k = F_k v$'dir. Her iki durumda da $P_{aux}\,t_k$ yardımcı yükü eklenir. Rejen sınırını aşan fren gücü
sürtünme frenine gider ve $E^{fric}$ olarak izlenir, çünkü yön asimetrisinin mekanizması budur.

*Düzlemsel* yüzeyde her θ sıfırdır ve başka hiçbir şey değişmez. Dolayısıyla iki yüzey arasındaki fark yalnızca
araziden gelir. Dört kapalı tur özdeşliği [`tests/test_energy.py`](tests/test_energy.py) içinde birim test olarak
yazılı:

1. Düz arazide DEM ve düzlem enerjisi çakışır.
2. Kayıpsız bir araç hiçbir kapalı turda enerji harcamaz.
3. Bir halkanın iki yönü kayıpsız araçta eşittir, aksi halde farklıdır.
4. Yuvarlanma ve aerodinamik direnç yokken simetrik bir tepe, düzlemdeki karşılığından tam olarak
   (1/η<sub>drive</sub> − η<sub>regen</sub>)·m·g·H fazlaya mal olur.

### Asimetrik enerji matrisi ve kesin en kısa yollar

Rejen varken kesim enerjisi negatif olabilir, Dijkstra ise negatif olmayan ağırlık ister. Artmeier vd. (2010) ve
Sachenbacher vd. (2011) izlenerek düğüm potansiyeli kullanılır:

```math
\pi(n) = \eta_{regen}\, m g\, h(n), \qquad e'_{ij} = e_{ij} + \pi(i) - \pi(j) \ge 0
```

Her yol boyunca düzeltmeler birbirini götürür, bu yüzden *e′* üzerinde Dijkstra en az enerjili yolu bulur. Raporlanan
enerji hiçbir zaman düzeltilmiş mesafe değildir; bulunan yol boyunca **gerçek** kesim enerjilerinin toplamıdır. Eğim
sınırının özdeşliği bozduğu birkaç kenar sıfıra kırpılır ve sayılır. `energy_options.exact_potentials=True` seçeneği
fiziksel potansiyelin yerine Bellman–Ford geçişinden Johnson potansiyellerini koyar. Ortaya çıkan duraktan durağa
`energy_matrix` (Wh) güçlü biçimde asimetriktir: bir durak çiftinin iki mesafesi eşittir, enerjileri eşit değildir.
[`tests/test_energy_routing.py`](tests/test_energy_routing.py) potansiyel düzeltmeli yolları ham enerjiler üzerindeki
Bellman–Ford sonucuyla karşılaştırır.

### Hat tasarımı

Aynı graftan üç matris kurulur: mesafe (m), süre (s) ve enerji (Wh). Biri amaçtır, diğer ikisi kısıt ve raporlama için
taşınır. Hat tasarımı aşağıdaki öğeleri içeren bir **kapalı turlu çok araçlı rotalama problemidir**:

- bugünkü gibi sekiz hatlık sabit filo;
- tek depo;
- her durağa bir zorunlu ziyaret;
- her mahalleyi tek hatta tutan aynı-araç kısıtı (küçük mahalleler en yakın büyük mahalleye katlanır);
- hat uzunluğu denge terimi.

Yolcu talebi ve araç kapasitesi modelde **yoktur** (bkz. revizyon deneyi E0). Çözücü Google OR-Tools'tur:
path-cheapest-arc ile başlangıç, guided local search ve sabit süre sınırı. Enerji maliyetleri tam Wh'ye yuvarlanır,
negatifse sabit bir değerle kaydırılır. Sabit filoyla her düğüm bir kez ziyaret edildiği için bu kaydırma hiçbir şeyi
değiştirmez. Hat içinde ziyaret sırası sekiz ve daha az durakta kesin aramayla, daha fazlasında 2-opt ve asimetrik
matris için ardından or-opt ile iyileştirilir.

### Bir kez optimize et, her yerde değerlendir

Her hattın gerçekleşen geometrisi kullandığı her yol parçasının hızıyla birlikte saklanır
(`metrics.routes[].edge_speeds`). Böylece bir çözüm **yeniden rotalanmadan herhangi bir yüzeyde yeniden sürülebilir**.
Ana karşılaştırmayı kesin yapan budur. E<sub>DEM</sub>(düzlem optimum ağ), düzlemsel çözümün kendi geometrisi, durak
sırası ve yol seçimleri arazi açılarak yeniden değerlendirildiğinde bulunan değerdir.

**Kodun koruduğu iki kural:**

- Yükseklik hiçbir geometriye yazılmaz (`flatten_to_2d`); (geometri, DEM sürümü) ikilisinden türetilir.
- Türetilen her değer `elevation_source`, `elevation_confidence` ve `grade_uncertainty_percent` taşır.

## Metrikler

| Metrik | Tanım |
|---|---|
| Tur enerjisi | E<sub>düzlem</sub>(R), E<sub>DEM</sub>(R): tüm hatların bir çevrimi için Wh; ayrıca hat başına |
| Düzlemsel tahmin hatası | E<sub>düzlem</sub> / E<sub>DEM</sub> − 1 |
| VoTI | E<sub>DEM</sub>(düzlem-opt ya da mesafe-opt) − E<sub>DEM</sub>(DEM-opt); Wh ve pay olarak |
| Üyelik benzerliği | Ortak durağı en çoklayan eşleştirmeyle eşlenen hatların ortalama Jaccard indeksi ve hat değiştiren durak sayısı |
| Sıra benzerliği | Eşlenen hatların ortak duraklarında ortalama Kendall τ |
| Yön asimetrisi | A = \|E<sub>→</sub> − E<sub>←</sub>\| / max(E<sub>→</sub>, E<sub>←</sub>); aynı çizgi ters yönde sürülerek |
| Sürtünme payı | E<sup>fric</sup> / E<sub>DEM</sub> |
| Analitik ceza kontrolü | E<sub>DEM</sub> − E<sub>düzlem</sub> ile (1/η<sub>drive</sub> − η<sub>regen</sub>)·m·g·Σtırmanış karşılaştırması |
| Eğik uzunluk oranı | Σℓ′ / Σℓ − 1; arazinin mesafenin kendisine olan (küçük) etkisi |
| Durak kararlılığı (Monte Carlo) | Bir durağın modal hattında kaldığı tekrarların payı |

## Deneyler

### Ana koşu `2026-09-29-urgup`

Ana koşuda 57 optimizasyon var. Hücre başına çözücü süresi 15 s, Monte Carlo tekrarlarında 5 s. Çıktılar
[`data/processed/experiments/2026-09-29-urgup/`](data/processed/experiments/2026-09-29-urgup/) altında.

| Deney | Ne yapar | Hizmet ettiği |
|---|---|---|
| **A** | Amaç (mesafe / düzlemsel enerji / DEM enerjisi) × η<sub>regen</sub> ∈ {0; 0,3; 0,5; 0,7} × kütle {boş, 7, 14 yolcu}. Mesafe ağı bir kez, düzlem ağı her kütle için bir kez, DEM ağı her hücre için optimize edilir. Her çözüm iki yüzeyde ve iki yönde değerlendirilir. | H1, H3 |
| **B** | A'daki her çözümün her hattı ve 8 referans hat, planlandığı yönde ve ters yönde | H2 |
| **C** | Rölyef depo kotu etrafında ölçeklenir, h′ = h<sub>ref</sub> + λ(h − h<sub>ref</sub>); λ ∈ {0; 0,5; 1; 1,5; 2} | H3 |
| **D** | Her biri kendi bozulmuş gridinde (σ = 4 m, uzamsal korele) 30 DEM enerji koşusu; çözümler hem o gridde hem referans gridde değerlendirilir | RQ5 |
| **E** | Belediyenin 8 hattı çizildiği haliyle, 12 (η<sub>regen</sub>, kütle) senaryosunun hepsinde | H1, H2 |
| **R** | Üç özdeş tekrar; denge ağırlığı 0; eğim sınırı 0,15 / 0,20 / 0,30 | Duyarlılık |

### Revizyon deneyleri `2026-10-R1`

Bunlar hakem aşaması için hazırlanan sağlamlık kontrolleridir. Rapor
[`docs/paper/revision_R1/REPORT.md`](docs/paper/revision_R1/REPORT.md) dosyasında; içindeki her tablo CSV dosyalarından
üretilir. Çıktılar [`data/processed/experiments/2026-10-R1/`](data/processed/experiments/2026-10-R1/) altında.

| Deney | Soru |
|---|---|
| **E0** | Model hangi kısıtları gerçekten uyguluyor? Araç kapasitesi kısıtı yok; üyelikleri mahalle kuralı belirliyor. |
| **E1** | Çözücü sağlamlığı: mesafe ve DEM enerjisi için 10 düğüm sırası tohumu × 15 / 60 / 300 s süre sınırı |
| **E2** | Mahalle kuralı kapalı; soğuk ve ılık başlangıç, ardışık arazi duyarlı arama |
| **E3** | DEM gürültüsü σ ∈ {1; 2; 2,5; 4} m × iki korelasyon uzunluğu (yaklaşık 90 m ve 300 m) × 30 tekrar |
| **E4** | Kesin en kısa yollar (Johnson / Bellman–Ford) ile kırpılmış fiziksel potansiyelin karşılaştırması |
| **E5** | Hat boyunca durak durak azalan teslimat yükü ve yüke duyarlı yeniden sıralama |
| **E6** | Ters yön tek yön kurallarına göre yasal mı? Yasal ters rotalama fiziksel ters yönle karşılaştırılır |
| **E7** | İkinci bir DEM (JAXA AW3D30) |

## Ön bulgular

Bu bulgular yer tutucu araç parametreleriyle elde edildi ve yalnızca yön ve mekanizma olarak veriliyor. Sayılar koşu
çıktılarında (`experiments_summary.csv`, `network_metrics.csv`) ve [R1 raporunda](docs/paper/revision_R1/REPORT.md).

**1. Düzlemsel model değerlendirilen her ağda enerjiyi küçümsüyor (H1 destekleniyor).** Bu hem optimize edilen ağlar
hem belediyenin bugün işlettiği hatlar için geçerli. Fark rejen verimi düştükçe ve kütle arttıkça büyüyor. Mekanizma
inişlerin eksik geri kazanılması; motorun hâlâ çektiği hafif inişlerde ise hiç geri kazanım yok. Aynı nedenle kesim
modelinin arazi cezası basit analitik sınırın altında kalıyor. Arazinin mesafenin kendisine etkisi ihmal edilebilir.

**2. Arazi bir durağa *hangi* hattın uğradığını değil, hattın *nasıl* sürüldüğünü değiştiriyor.** Belediyenin mahalle
kuralı altında hat üyeliğini kuralın kendisi belirliyor (E0). Arazi bilgisinin değeri bu yüzden ziyaret sırasından ve
duraklar arasındaki yol seçiminden geliyor. Arazi optimum ağ biraz daha uzun, ama daha az tırmanıyor ve inişleri rejen
sınırının altında tutuyor. VoTI farklı çözücü başlangıçlarında ve süre sınırlarında (E1), kesin en kısa yollarla (E4),
ikinci bir DEM'de (E7) ve azalan teslimat yüküyle (E5) pozitif kalıyor.

**3. Karar değeri rölyefle muhasebe hatasından daha hızlı büyüyor.** Rölyef düzden Ürgüp'ün iki katına
ölçeklendiğinde muhasebe hatası kabaca doğrusal artıyor ve doyuyor. VoTI ise kabaca karesel artıyor. Üyelik
değişikliği yalnızca taramanın üst ucunda görülüyor. H3 bu ağda karar düzeyinde destekleniyor, üyelik düzeyinde
reddediliyor.

<p align="center">
  <img src="docs/paper/figures/fig08_voti_vs_scale.png" alt="Arazi yoğunluğuna göre arazi bilgisinin değeri, düzlemsel küçümseme ve üyelik benzerliği" width="640">
  <br><sub><b>Makalenin Şekil 8'i.</b> Arazi yoğunluğu taraması. Değerler yer tutucu araç parametreleriyle elde edilmiş ön değerlerdir.</sub>
</p>

**4. Daha iyi bir aktarma organı tahmini daha az yanlış, kararı daha değerli yapıyor.** Yüksek rejen verimi düzlemsel
hatayı küçültüyor. Arazi duyarlı tasarımın kullanabileceği, güç sınırından kaynaklanan asimetriyi ise büyütüyor.

**5. Kapalı bir halkanın ucuz bir yönü var (H2 destekleniyor).** Bir halkanın iki yönü mesafede eşit, enerjide eşit
değil. Fark birkaç dik hatta yoğunlaşıyor ve pahalı yönde fren gücünün daha büyük kısmı sürtünme frenine gidiyor.
Bugünkü hatlar ana yolları orta eğimlerle izliyor ve yöne neredeyse duyarsız. E6'dan gelen bir çekince var: tek yön
kuralları altında hiçbir hat olduğu gibi ters yönde sürülemiyor. Yasal yoldan rotalanan ters yön asimetriyi
değiştiriyor ve hat hat değerlendirilmesi gerekiyor.

**6. DEM, muhasebe için değil karar için zayıf halka.** Ürünün %90 hata sınırında, kısa korelasyonlu kötümser
gürültüyle gürültülü gridde optimize edilen ağlar kendi gridinde daha iyi, gerçek arazide daha kötü görünüyor. E3, karar
değerinin daha küçük hatalarda ve daha uzun mesafede korele hatalarda korunduğunu gösteriyor. Arazi duyarlı *tahmin* her
durumda sağlam.

**7. Mahalle kuralı kapalıyken üyelikler değişiyor (E2).** Bu durumda bağımsız sezgisel koşulara arama gürültüsü
hâkim oluyor. Düzlemsel ağdan başlatılan ardışık arazi duyarlı arama yine de birkaç durağı hatlar arasında taşıyarak
enerji kazandırıyor. Bu değişikliklere "araziden kaynaklanan" diyebilmek için daha uzun ya da kesin bir arama gerekiyor;
bu açık bir soru olarak kalıyor.

**Uygulamaya etkisi.** Öneri sıralı:

1. Yükseklik katmanını önce enerji muhasebesinin altına koyun: batarya boyutlandırma, menzil ve şarj planı.
2. Ardından tek yön kuralları içinde her dik kapalı turun yönünü seçmek için kullanın.
3. Sıra ve yol seçimini ancak rölyef güçlüyse ve yükseklik verisi küresel bir yüzey modelinden iyiyse değiştirin;
   kazancı benimsemeden önce bozulmuş bir araziye karşı sınayın.

<p align="center">
  <img src="docs/paper/figures/fig05_asymmetry.png" alt="DEM optimum hatların yön asimetrisi" width="720">
  <br><sub><b>Makalenin Şekil 5'i.</b> Arazi optimum hatların yön asimetrisi; taralı kısım sürtünme freni kaybı.</sub>
</p>

## Kapsam ve sınırlılıklar

- **Kapsam dışı:** durak sayısını azaltma, sefer sıklığı, headway, araç sayısı, yolcu talebi ve araç kapasitesi.
  Sonuçlar her hattın **bir çevrimi** için raporlanır, bu yüzden sıklık varsayımı gerekmez.
- **Araç parametreleri yer tutucudur** ve sınıf için raporlanan aralıklar içindedir. η<sub>regen</sub> ve kütle
  taramaları okurun gerçek bir aracı sonuçlar içinde konumlamasını sağlar.
- Hız yolun yasal ya da varsayılan hızıdır ve kesim boyunca sabittir. İvmelenme yalnızca duraklarda modellenir;
  rejen sınırının altında kalmak için serbest yuvarlanma modellenmez.
- Çözücü süre sınırlı bir sezgiseldir, bu yüzden "optimum" her zaman "bulunan en iyi" demektir.
- Bugünkü hatlar çizilmiş geometrileri üzerinden 30 km/sa ile değerlendirilir. Sekiz çizimden dördü parçalı
  zincirlerdir.
- Fiziksel ters yön tek yön kurallarını bilerek yok sayar; yasallık E6'da ayrıca sınanır. Dönüş yasakları modellenmez.
- Bu, iki 30 m'lik DEM ile tek bir kentte yapılmış bir vaka çalışmasıdır. Genellenmesi beklenen kısım eğrilerin
  *biçimidir*.

---

# Tekrar üretim

## Paketin içeriği

| Yol | İçerik |
|---|---|
| [`urgup_transport/energy.py`](urgup_transport/energy.py) | Kesim enerji modeli: yuvarlanma, aerodinamik, eğim ve yardımcı yük; rejen verimi ve güç sınırıyla. Salt standart kütüphane |
| [`urgup_transport/elevation.py`](urgup_transport/elevation.py) | Bir geometri boyunca griddeki yüksekliği okur, geometriye yazmaz |
| [`urgup_transport/optimizer.py`](urgup_transport/optimizer.py) | Mesafe, süre ve enerji matrisleri (potansiyel düzeltmeli Dijkstra), OR-Tools rotalama, rota gerçekleştirme |
| `urgup_transport/…`, `webapp/optimization_service.py` | Optimizer'ın içe aktardığı veri katmanı ve yardımcı modüller |
| [`scripts/run_terrain_experiments.py`](scripts/run_terrain_experiments.py) | Ana koşu: A–E ve R deneyleri |
| [`scripts/run_r1_revision_experiments.py`](scripts/run_r1_revision_experiments.py) | E0–E7 revizyon deneyleri; ayrıca `report` ve `figures` |
| [`scripts/plot_terrain_figures.py`](scripts/plot_terrain_figures.py) | Koşu CSV'lerinden Şekil 1–9 |
| [`scripts/build_elevation.py`](scripts/build_elevation.py) | Copernicus GLO-30'u indirip yükseklik gridini kurar |
| [`tests/`](tests/) | Enerji modelinin dört kapalı tur özdeşliği ve Bellman–Ford'a karşı enerji rotalaması |
| `data/editable/network.json` | Girdi: durak kaydı, mahalleler ve yayınlanan hatlar (taslak revizyonu 135) |
| `data/editable/road_network.geojson` | Girdi: yönlü yol grafiği (revizyon 1.584) |
| `data/processed/baseline/baseline_network.json` | Girdi: bugün işletilen 8 hat, dondurulmuş |
| `data/parameters/vehicle_profiles.json` | Araç parametreleri (yer tutucu) |
| `data/processed/experiments/2026-09-29-urgup/` | Ana koşunun çıktıları |
| `data/processed/experiments/2026-10-R1/` | Revizyon koşularının çıktıları |
| `docs/paper/figures/`, `docs/paper/revision_R1/` | Bu çıktılardan çizilen şekiller ve tablolar; R1 raporu |
| [`SOURCE.json`](SOURCE.json), [`SHA256SUMS`](SHA256SUMS) | Dışa aktarımın kaynağı ve her dosyanın sağlama toplamı |

Kod, yazarların Ürgüp Belediyesi için geliştirdiği planlama platformundan dışa aktarıldı; platform public değil. Pakette
yalnızca deneylerin ihtiyaç duyduğu kısım var ve klasör adları kodun beklediği adlardır.

## Kurulum

**Gereksinimler.** Python 3.12 ya da üstü; paket 3.12.3 ve 3.14.4 üzerinde sınandı. Yayınlanan koşular Linux üzerinde
Python 3.14.4 ile yapıldı; OR-Tools [`requirements.txt`](requirements.txt) içinde `9.15.6755` sürümüne sabitli.

```bash
git clone https://github.com/elestirmen/terrain-matters.git
cd terrain-matters
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> [!IMPORTANT]
> `DATABASE_URL` tanımlı **olmamalı**. Tanımlı değilse kod girdilerini `data/editable/` altından okur. Başka dosyalarla
> koşmak için ana koşu betiği `--draft`, `--roads` ve `--reference` seçeneklerini de alır.

## Enerji modelini sınama

```bash
python -m unittest tests.test_energy tests.test_energy_routing
```

Testler saniyeler sürer. Modelin dört kapalı tur özdeşliğini (bkz. [Yöntem](#kesim-enerji-modeli)) ve potansiyel
düzeltmeli en kısa yolların ham enerjiler üzerindeki Bellman–Ford sonucuna eşit olduğunu sınarlar.

## Yükseklik gridini kurma

Grid üretilir ve git dışında tutulur; sağlama toplamı her koşu manifestinde yazılıdır.

```bash
python scripts/build_elevation.py          # Copernicus GLO-30 → data/processed/elevation/glo30-v1/
```

Betik kenti kapsayan iki Copernicus GLO-30 paftasını indirir. Koşu manifestleri kullandıkları gridin sha256 değerini
kaydeder (`manifest.json` içinde `elevation.sha256`); yeniden kurulan grid bu değerle eşleşmelidir. E7 deneyi AW3D30
gridini (`data/processed/elevation/aw3d30-v1/`) Microsoft Planetary Computer'dan indirir.

## Deneyleri yeniden koşma

**1. Ana koşu ve şekiller.** 57 optimizasyondan oluşur; her biri 15 s, Monte Carlo tekrarları 5 s. Yayınlanan koşunun
devamı gibi okunmaması için yeni bir koşu kimliği verin:

```bash
python scripts/run_terrain_experiments.py --run-id benim-kosum --experiments A,B,C,D,E,R
python scripts/plot_terrain_figures.py --run-id benim-kosum --out benim-sekillerim   # baskı için --dpi 600
```

`data/processed/experiments/benim-kosum/network_metrics.csv` dosyasını yayınlanan
`2026-09-29-urgup/network_metrics.csv` ile karşılaştırın. Yayından önce A deneyi (16 optimizasyon) bu paketin temiz
bir kurulumundan Python 3.14 ile yeniden koşuldu ve 4 çekirdekli bir Intel N100'de 5 dk 21 s sürdü. 36 ağ satırının ve
576 hat satırının hepsi, her koşuda yeniden üretilen hat kimlikleri dışında, yayınlanan koşuyla birebir aynı çıktı.

**2. Revizyon deneyleri.** Toplamda yaklaşık 3,5 saat çözücü süresi tutar; yalnızca E1 yaklaşık 1,7 saattir. Betik
kayıtlı hücrelerden devam eder; yeniden okumak yerine hesaplamak için yayınlanan klasörü kenara alın:

```bash
mv data/processed/experiments/2026-10-R1 data/processed/experiments/2026-10-R1.published
python scripts/run_r1_revision_experiments.py E1      # E1 … E7, birer birer
python scripts/run_r1_revision_experiments.py report  # CSV'lerden docs/paper/revision_R1/REPORT.md
python scripts/run_r1_revision_experiments.py figures # CSV'lerden figR*.png
```

## Çıktılar

Her ana koşu `data/processed/experiments/<run_id>/` altına şunları yazar:

| Dosya | İçerik |
|---|---|
| `manifest.json` | Koşu kimliği, platform commit'i, DEM sürümü ve sha256'sı, araç profili ve sha256'sı, bütün parametreler, süreler |
| `proposals/<hücre>.json` | Her hücrenin tam optimizer önerisi |
| `routes.geojson` | Her hücrenin hat geometrisi, hücre etiketiyle |
| `route_metrics.csv` | (hücre, değerlendirme senaryosu, hat, yön) başına bir satır |
| `network_metrics.csv` | (hücre, değerlendirme senaryosu) başına bir satır: toplamlar, VoTI, Jaccard, τ |
| `reference_metrics.csv` | Deney E: belediyenin hatları, çizildiği haliyle |
| `monte_carlo.csv`, `stop_stability.csv` | Deney D |
| `experiments_summary.csv` | Şekillerin çizildiği her şey |

Revizyon koşuları her deney için bir klasör yazar (`2026-10-R1/E1/` … `E7/`); her birinde kendi `manifest.json`
dosyası ve CSV'leri var. Aynı `--run-id` ile yeniden koşmak bitmiş hücreleri okur ve yalnızca eksikleri çözer.

## Şekiller ve tablolar

Her şekil yukarıdaki CSV dosyalarından bir betikle çizilir; hiçbir sayı elle yazılmaz.

| Şekil | Dosya | Gösterdiği |
|---|---|---|
| 1 | [`fig01_terrain_network.png`](docs/paper/figures/fig01_terrain_network.png) | Copernicus GLO-30 üzerinde yol ağı ve duraklar |
| 2 | [`fig02_route_profile.png`](docs/paper/figures/fig02_route_profile.png) | DEM optimum ağın en dik hattı boyunca yükseklik ve eğim |
| 3 | [`fig03_segment_energy.png`](docs/paper/figures/fig03_segment_energy.png) | Yönlü yol parçası başına enerji ve çift yönlü parçaların yön asimetrisi |
| 4 | [`fig04_penalty_vs_eta.png`](docs/paper/figures/fig04_penalty_vs_eta.png) | η<sub>regen</sub>'e göre düzlemsel küçümseme, arazi cezası ve VoTI (Deney A) |
| 5 | [`fig05_asymmetry.png`](docs/paper/figures/fig05_asymmetry.png) | DEM optimum hatların yön asimetrisi (Deney B) |
| 6 | [`fig06_reference_lines.png`](docs/paper/figures/fig06_reference_lines.png) | Bugünkü sekiz hat: düzlem, çizildiği yönde DEM ve ters yönde DEM (Deney E) |
| 7 | [`fig07_networks.png`](docs/paper/figures/fig07_networks.png) | Düzlem optimum ve DEM optimum ağlar: aynı duraklar, aynı çözücü, iki yüzey |
| 8 | [`fig08_voti_vs_scale.png`](docs/paper/figures/fig08_voti_vs_scale.png) | Arazi yoğunluğu taraması (Deney C) |
| 9 | [`fig09_montecarlo.png`](docs/paper/figures/fig09_montecarlo.png) | DEM gürültüsü altında VoTI, tur enerjisi ve üyelik kararlılığı (Deney D) |

R1 raporu, tabloları (CSV) ve `figR1`–`figR5` şekilleri [`docs/paper/revision_R1/`](docs/paper/revision_R1/) altında.

## Tekrar üretilebilirlik notları

- **Girdiler.** Girdiler yayınlanan koşunun çözüldüğü girdilerin aynısıdır. Taslak revizyonu koşu manifestiyle, yol ağı
  her önerinin kaydettiği özetle karşılaştırıldı. Sonuç [`SOURCE.json`](SOURCE.json) içinde.
- **Çözücü.** OR-Tools bu örnekte yinelenen koşularda özdeş ağlar verdi. 15 s ve 60 s sınırları da aynı ağı verdi,
  çünkü guided local search 15 s'den çok önce duruyor. Bu yüzden makine hızının etkisi küçük olmalı, ama farklı bir
  OR-Tools sürümü arama yolunu değiştirebilir.
- **Rastgelelik.** Monte Carlo ve düğüm sırası tohumları betiklerde sabit ve her manifestte kayıtlı.
- **Manifestler.** Her koşunun `manifest.json` dosyası platform commit'ini, DEM sağlama toplamını, araç profilini ve
  onun sağlama toplamını, bütün parametreleri kaydeder.
- **Optimum değil, bulunan en iyi.** Çözücü süre sınırlı bir sezgiseldir, bu yüzden bir çözüm "optimum" değil, "bulunan
  en iyi"dir.
- **Bütünlük.** `sha256sum -c SHA256SUMS` paketteki her dosyayı denetler.

## Veri ve lisanslar

| Kısım | Lisans |
|---|---|
| Kod | MIT ([`LICENSE`](LICENSE)) |
| Yol geometrisi (`data/editable/road_network.geojson`, her koşunun `routes.geojson` ve `proposals/*.json` dosyaları) | ODbL 1.0 ([`LICENSE-ODbL`](LICENSE-ODbL)), © OpenStreetMap katkıcıları |
| Diğer veri, tablolar ve şekiller | CC BY 4.0 ([`LICENSE-DATA`](LICENSE-DATA)) |

Yol grafiği OpenStreetMap'ten türetildi ve Ürgüp Belediyesi'nin yol orta hatlarıyla tamamlandı. Duraklar ve işletilen
hatlar belediyenin açık verisinden geliyor. Yükseklik gridleri yeniden dağıtılmaz; Copernicus DEM GLO-30 © DLR e.V. ve
Airbus'tır, COPERNICUS kapsamında Avrupa Birliği ve ESA tarafından sağlanır. Kaynaklar ve zorunlu atıf metinleri
[`ATTRIBUTION.md`](ATTRIBUTION.md) dosyasında.

## Atıf, yazarlar ve yapay zekâ kullanımı

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

| Yazar | Kurum | ORCID |
|---|---|---|
| **Ahmet Ertuğrul Arık** (ilk yazar) | Bilişim Sistemleri ve Teknolojileri Bölümü, Kapadokya Üniversitesi, Ürgüp, Nevşehir | [0000-0002-7952-4311](https://orcid.org/0000-0002-7952-4311) |
| **M. Ali Ülkü** (sorumlu yazar) | Department of Management Science and Information Systems ve Centre for Research in Sustainable Supply Chain Analytics (CRSSCA), Faculty of Management, Dalhousie University, Halifax, Kanada | [0000-0002-8495-3364](https://orcid.org/0000-0002-8495-3364) |

**Üretken yapay zekâ kullanımı.** Enerji modeli, deney betiği ve şekil betikleri yazarların tasarımı ve denetimi
altında Claude Code (Anthropic) ile yazıldı. Her denklemi, testi ve sonucu yazarlar doğruladı. Bu sayfanın başındaki
görsel üretilmiş bir çizimdir ve makalenin bir şekli değildir. Makaledeki bütün şekil ve tablolar deney çıktılarından
çizilir.

**Teşekkür.** Yol orta hatları, durak kaydı ve güzergâh kayıtları Ürgüp Belediyesi'nden alındı.
