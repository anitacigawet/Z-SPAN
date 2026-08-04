[PANSAMANTALANG DRAFT NA ISINULAT NG AI. MULING ISUSULAT PAGSAPIT NG AGOSTO 4, 2026]

# Z-SPAN

[English](README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [**Filipino**](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**Isang virtual na aklatan tungkol sa lokal na politika.**

[Bisitahin ang Z-SPAN sa zspan.org](https://zspan.org)

✨ **Inilathala para masuri, mapanatili, at mapagkunan ng ideya.**

Layunin ng Z-SPAN na gawing mas madaling hanapin, panoorin, at unawain ang mga
pampublikong pulong ng lokal na pamahalaan. Ang mga lugar ay nagiging mga
channel, ang mga pulong ay nagiging mga episode, at nananatiling bahagi ng
paglalakbay ang orihinal na video, agenda, at katitikan.

Ang repository na ito ang aklatan sa likod ng aklatan: isang piling koleksiyon
ng pampublikong source code, mga paraan ng proyekto, at mga aral na maaaring
makatulong sa sinumang nag-iisip ng kahalintulad na proyekto sa ibang lungsod,
estado, o bansa.

Hindi ito kumpletong kopya ng sistemang ginagamit sa produksiyon at hindi ito
nilalayong i-clone at ilunsad bilang panibagong Z-SPAN. Mas maliit ang
kapaki-pakinabang na bahagi rito: isang ideya sa pag-navigate, isang
malinaw na hangganan para sa pagpapatugtog ng video, isang paraan upang manatiling
nakikita ang mga pinagkunan, o isang prinsipyo sa disenyo na maaaring dalhin sa
isang hiwalay na proyekto.

> Ang pahinang ito ay salin ng English README na ginawa sa tulong ng AI.
> Malugod na tinatanggap ang mga pagwawasto sa pamamagitan ng pull request mula
> sa mga bihasa sa Filipino. Kung may pagkakaiba sa kahulugan, ang
> [English README](README.md), [LICENSE](LICENSE), at [NOTICE](NOTICE) ang
> masusunod. Nasa English pa ang iba pang naka-link na dokumento.

---

## 📚 Bakit may ganitong aklatan

Madalas na pare-pareho ang mga tanong na kinakaharap ng mga proyektong
gumagamit ng lokal na pampublikong rekord:

- Paano maghahanap ng mga pulong kung magkakaiba ang pagkakaayos ng bawat
  website ng pamahalaan?
- Paano mananatiling kapaki-pakinabang ang iisang interface sa iba't ibang
  lungsod at video provider?
- Paano mananatiling malinaw ang daan pabalik sa opisyal na pinagmulan?
- Paano maipaliliwanag ng isang teknikal na sistema ang sarili nito nang hindi
  ipinababasa sa mga tao ang database sa ilalim nito?

Ang Z-SPAN ay isang gumaganang sagot, hindi ang tanging sagot. Layunin ng
repository na panatilihing sapat na nakikita ang mga kapaki-pakinabang na ideya
upang masuri, makuwestiyon, at higit pang mapaunlad ng ibang mga proyekto.

## 👋 Para kanino ang aklatang ito

Mag-aaral, aktibista, mamamahayag, mananaliksik, designer, developer,
boluntaryo, o sadyang interesado ka man sa lokal na pampublikong impormasyon,
hindi mo kailangang gamitin ang buong proyekto upang may mapakinabangan dito.
Nakaayos ang aklatan upang maunawaan ang isang ideya o bahagi sa bawat
pagkakataon.

## 🧭 Paano gamitin ang repository na ito

Walang kailangang sunding pagkakasunod-sunod, ngunit makatutulong ang mga
panimulang ito:

1. Basahin ang [modelo ng proyekto](docs/PROJECT_MODEL.md) para sa pinakasimpleng
   paliwanag kung paano magkakaugnay ang mga bahagi.
2. Buksan ang [katalogo ng aklatan](CATALOG.md) upang pumili ng bahagi ng code,
   prompt, o disenyo ayon sa tanong na nais mong siyasatin.
3. Tingnan ang [mga paraang maaaring dalhin sa ibang proyekto](docs/DESIGN_PATTERNS.md)
   para sa mga ideya sa likod ng interface.
4. Gamitin ang [gabay sa repository](docs/REPOSITORY_GUIDE.md) upang sundan ang
   isang partikular na paglalakbay ng bisita sa inilathalang source code.
5. Suriin [kung ano ang inilathala at hindi inilathala](PUBLICATION_SCOPE.md)
   bago bumuo ng konklusyon tungkol sa mas malawak na sistema ng Z-SPAN.
6. Tingnan ang [kasalukuyang tala ng snapshot](docs/snapshots/2026-08-02.md)
   para sa eksaktong laki at katayuan ng pagsusuri ng release na ito.

## 🗂️ Ano ang nasa koleksiyon

Kasalukuyang ipinapakita ng inilathalang source code ang anim na bahagi ng
karanasan ng bisita:

- **Paghahanap ng lugar o pulong** sa pamamagitan ng home, channel, lungsod,
  at search view.
- **Pagtingin sa mga available** sa pamamagitan ng gabay na lumilipat sa mga
  card, mapa, naka-embed na player, at mas malaking view.
- **Pagbalik sa orihinal na rekord** sa pamamagitan ng malinaw na link sa
  opisyal na video, agenda, at katitikan kapag available.
- **Pag-play ng video sa iisang interface** kahit magbago ang pinagmumulang
  video host.
- **Pagpapaliwanag sa bisita ng mga pagsusuri sa integridad** sa pamamagitan ng audit,
  scan, at verification view.
- **Pagbuo ng madaling basahing buod ng gawaing sibiko mula sa rekord ng
  pulong** sa pamamagitan ng tatlong nasuring halimbawa sa prompt shelf.

[MAGDADAGDAG DITO NG BISWAL NA PAGLALAHAD SA HINAHARAP]

Iniuugnay ng [gabay sa repository](docs/REPOSITORY_GUIDE.md) ang bawat ideya sa
kaukulang mga file.

## Paalala tungkol sa pagpapatakbo ng code

Walang gabay sa installation, hosting, Docker, o deployment sa repository na
ito. Sinadya iyon.

Pinili ang mga inilathalang file mula sa mas malaking pribadong sistemang
aktibong ginagamit. Hindi kasama ang ilan sa mga import, serbisyo, pagkakabit ng
application, at runtime configuration nito. Narito ang source code upang
basahin at pag-aralan; hindi ito inihaharap bilang isang standalone na
application o suportadong distribution.

## Paano nakaayos ang repository

- Ipinapaliwanag ng [`docs/`](docs/) ang modelo ng proyekto, mga paraang maaaring
  muling gamitin, mga ruta sa pagbasa, at may-petsang pampublikong snapshot.
- Nasa [`code/`](code/) ang piling reference code ng visitor interface, na
  inayos nang hiwalay sa pribadong landas ng working project.
- Nasa [`prompts/`](prompts/) ang tatlong nasuri at hindi binagong halimbawa ng
  prompt na maaaring pag-aralan o iangkop nang paisa-isa.
- Ang [`CATALOG.md`](CATALOG.md) ang indeks ng bawat bahagi para sa mga tao at
  AI reader.
- Malinaw na inilalarawan ng [`PUBLICATION_SCOPE.md`](PUBLICATION_SCOPE.md) ang
  hangganan ng pampublikong paglalathala.

Mga pangalan lamang ng mga bahagi ang binabago ng public export. Pinananatili
ang relatibong ayos sa loob ng `code/visitor-interface/src/` upang mabasa pa rin
ang ugnayan ng mga page, component, player adapter, at style.

## ⚖️ Lisensiya

Ang inilathalang code ay available sa ilalim ng
[PolyForm Noncommercial License 1.0.0](LICENSE). Maaari itong pag-aralan,
iangkop, ibahagi, at muling gamitin para sa mga layuning hindi pangkomersiyo
ayon sa mga tuntunin ng lisensiya. Kabilang dito ang pansariling pag-aaral,
hobby project, edukasyon, pampublikong pananaliksik, gawaing pangkawanggawa,
at paggamit ng pamahalaan.

Hindi pinahihintulutan ng lisensiyang ito ang komersiyal na paggamit. Nasa
[NOTICE](NOTICE) ang kinakailangang pagkilala at hangganan sa paggamit ng
pangalan ng Z-SPAN.

## Makipag-ugnayan

Naka-host ang proyekto sa [zspan.org](https://zspan.org). Kung interesado kang
punan ang isang bukas na puwesto sa Z-SPAN ecosystem, makipag-ugnayan sa
[anitacigawet@pm.me](mailto:anitacigawet@pm.me) para sa karagdagang impormasyon.
