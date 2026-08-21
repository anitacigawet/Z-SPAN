<p align="center">
  <img src="../repository-assets/banner-doodle.png" alt="Z-SPAN untuk semua. Perpustakaan virtual tentang politik lokal. Dipelihara oleh masyarakat, untuk masyarakat." width="1000">
</p>

> *Scientia potentia est.*
>
> **Pengetahuan adalah kekuatan.**
>
> — Francis Bacon

---

[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [**Bahasa Indonesia**](README.id.md) · [Filipino](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**Perpustakaan virtual tentang politik lokal.**

[Kunjungi Z-SPAN di zspan.org](https://zspan.org)

✨ **Diterbitkan sepenuhnya, untuk siapa saja. Dikembangkan dengan bantuan siapa saja.**

Z-SPAN adalah upaya untuk membuat rapat publik daerah lebih mudah ditemukan,
ditonton, dan dipahami. Wilayah menjadi kanal, rapat menjadi episode, sedangkan
video, agenda, dan notulen asli tetap menjadi bagian dari jalurnya.

Repositori ini memuat perpustakaan kerja itu sendiri: situs web, API publik,
parser sumber rapat, alur pemrosesan, klien lokal, serta pemeriksaan yang menjaga
hasil kerja tetap terikat pada arsip publik. Alasan seluruh perangkat ini
diterbitkan sederhana: perpustakaan yang dipelihara satu orang akan berakhir
bersama orang tersebut. Perpustakaan yang dapat diperiksa, dijalankan,
dipertanyakan, dan diteruskan oleh orang lain tidak akan demikian.

Daftar sumber rapat pemerintah berada di repositori terpisah,
[National Civics Catalog](https://github.com/anitacigawet/national-civics-catalog).
Repositori tersebut memuat endpoint publik yang berkelanjutan beserta buktinya—
bukan parser, transkrip, ringkasan, atau rapat yang telah diproses oleh Z-SPAN.
Z-SPAN adalah salah satu contoh yang dapat dibangun dari katalog itu.

## Tonton panduan lengkap

[![Tonton “Z-SPAN Is Born” — panduan lengkap proyek Z-SPAN](https://i.ytimg.com/vi/HTpR9jRl314/hqdefault.jpg)](https://www.youtube.com/watch?v=HTpR9jRl314)

[**Z-SPAN Is Born**](https://www.youtube.com/watch?v=HTpR9jRl314) menjelaskan
perpustakaan awal Arizona dari sudut pandang pengelolanya. Tonton untuk melihat
gambaran awal Z-SPAN, bagaimana bagian-bagiannya saling terhubung, dan apa yang
hendak diteruskan melalui jalur publik ini.

## 🗺️ Direktori nasional, dibangun tempat demi tempat

Arizona adalah bukti konsep publik yang saat ini diproses dan diterbitkan oleh
Z-SPAN. Direktori kanal juga memberi setiap negara bagian dan teritori suatu
bentuk awal yang nyata, tersusun berdasarkan badan publik tingkat negara
bagian, wilayah setara county, Tribal, regional, dan lokal.

Rak hijau berisi rapat Z-SPAN yang sudah diterbitkan. Rak ambar adalah pekerjaan
yang masih berlangsung dan ditampilkan apa adanya: tempatnya sudah ada di
direktori, tetapi sumber rapat berkelanjutan atau parser Z-SPAN-nya masih perlu
ditangani. Siapa pun dapat membantu komunitasnya sendiri tanpa menunggu undangan.

## 🐈 Bantu kota asal Anda

1. Temukan negara bagian dan tempat Anda di [zspan.org](https://zspan.org).
2. Jika raknya masih menunggu, klik kucing yang sedang tidur.
3. Salin petunjuk singkat berformat Markdown ke asisten AI yang sudah Anda
   gunakan.
4. Jawab beberapa pertanyaan biasa tentang tempat tersebut dan halaman rapat
   resminya. Anda tidak perlu memahami JSON atau Git.
5. Jika alat GitHub tersedia, asisten dapat menyiapkan pull request yang
   terfokus untuk Anda konfirmasi. Jika tidak, asisten menyiapkan laporan
   lengkap untuk dikirim melalui formulir GitHub yang sederhana.

Kontribusi masuk ke National Civics Catalog, tempat pemeriksa tepercaya dan
seorang manusia meninjau endpoint beserta buktinya. Kontribusi tidak pernah
diterbitkan langsung ke Z-SPAN.

**Janji tiga hari Z-SPAN:** setelah kontribusi katalog diterima, Z-SPAN akan
membuat parser yang sesuai atau menampilkan hasil yang jelas bahwa sumbernya
terhalang, dalam waktu tiga hari. Janji ini adalah tentang menjadikan sumber
dapat digunakan atau menjelaskan dengan jujur mengapa sumber itu belum dapat
digunakan—bukan menerbitkan konten rapat buatan AI secara otomatis.

[Baca petunjuk kontribusi dengan AI](https://github.com/anitacigawet/national-civics-catalog/blob/main/contribute/AI-INSTRUCTIONS.md)

## 📚 Mengapa perpustakaan ini ada

Proyek yang bekerja dengan arsip publik daerah cenderung menghadapi pertanyaan
yang sama:

- Bagaimana seseorang dapat menelusuri rapat ketika situs pemerintah
  mengaturnya dengan cara yang berbeda-beda?
- Bagaimana satu antarmuka dapat tetap berguna di berbagai tempat dan penyedia
  video?
- Bagaimana jalur kembali ke sumber resmi dapat selalu terlihat jelas?
- Bagaimana sistem teknis dapat menjelaskan dirinya tanpa memaksa orang
  membaca basis data di baliknya?

Z-SPAN adalah salah satu jawaban yang dapat diterapkan, bukan satu-satunya.
Tujuan repositori ini adalah membuat keseluruhannya tetap terlihat—agar dapat
diperiksa, dipertanyakan, dan diteruskan oleh orang-orang yang menggunakannya.

## 👋 Untuk siapa perpustakaan ini

Baik Anda pelajar, pegiat masyarakat, jurnalis, peneliti, desainer,
pengembang, relawan, maupun sekadar ingin tahu tentang informasi publik lokal,
Anda tidak perlu mengadopsi seluruh proyek untuk menemukan sesuatu yang
berguna di sini. Perpustakaan disusun agar satu gagasan atau komponen dapat
dipahami pada satu waktu—dan satu tempat dapat ditambahkan pada satu waktu.

## 🗂️ Susunan repositori ini

- [`council_navigator`](../02_Core_Project/council_navigator/) — situs web, API
  publik, cache rapat lokal, dan direktori kanal publik.
- [`parsers`](../02_Core_Project/council_navigator/parsers/) — parser kalender
  khusus sumber yang mengubah endpoint katalog menjadi bentuk rapat yang sama.
- [`zspan_pipeline`](../02_Core_Project/zspan_pipeline/) — antrean pemrosesan
  yang mengubah rekaman rapat menjadi materi berbasis sumber dan dapat ditinjau.
- [`zspan_cli`](../02_Core_Project/zspan_cli/) — klien lokal untuk menggunakan
  Z-SPAN dari komputer dan ruang kerja milik seseorang.
- [`prompts`](../02_Core_Project/prompts/) — kontrak sintesis terbitan yang
  digunakan dalam jalur pemrosesan.

National Civics Catalog tetap menjadi repositori terpisah agar orang dapat
memperbaiki direktori sumber tanpa mengubah aplikasi Z-SPAN, dan agar proyek
lain dapat menggunakan endpoint yang sama untuk tujuan yang sama sekali berbeda.

## Komitmen proyek ini

Berikut adalah batasan yang dipegang proyek, bukan sekadar cita-cita:

- **Tidak ada editorial tentang pejabat publik.** Ucapan mereka ditampilkan
  apa adanya, dengan atribusi dan sumber. Penilaiannya ada di tangan Anda.
- **Tidak ada pengumpulan data tentang warga perorangan.** Pejabat yang sedang
  menjalankan peran publiknya adalah subjek pekerjaan ini; warga yang berbicara
  di mikrofon publik tidak dibuatkan profil.
- **Membaca tidak pernah dibatasi.** Tidak diperlukan paywall, langganan,
  halaman login, atau pendaftaran untuk membaca konten arsip publik yang telah
  diterbitkan.
- **Tidak ada optimisasi keterlibatan.** Tidak ada umpan tanpa akhir,
  algoritme rekomendasi, atau mekanisme pemicu kemarahan. Arsip ini sengaja
  dibuat tenang.
- **Seorang manusia meninjau sebelum apa pun diterbitkan.** Pemrosesan dapat
  diotomatisasi; penerbitan tidak.
- **Nonkomersial sejak dari rancangan.** Lisensi menjadikan batas ini bagian
  dari strukturnya.

## 🏛️ Pengelolaan awal

Z-SPAN dimulai di Arizona dan dipelihara oleh
[@anitacigawet](https://github.com/anitacigawet). Kontribusi pada direktori
sumber diberi kredit di National Civics Catalog; implementasi Z-SPAN ditinjau
dan dipelihara secara terpisah di sini.

## ⚖️ Lisensi

Kode yang diterbitkan tersedia di bawah
[PolyForm Noncommercial License 1.0.0](../LICENSE). Kode dapat dipelajari,
disesuaikan, dibagikan, dan digunakan kembali untuk tujuan nonkomersial sesuai
dengan ketentuan lisensi. Ini mencakup pembelajaran pribadi, proyek hobi,
pendidikan, penelitian publik, kegiatan amal, dan penggunaan pemerintah.

Lisensi ini tidak memberikan izin untuk penggunaan komersial. Pemberitahuan
yang diwajibkan dan batas penggunaan nama Z-SPAN tercatat dalam
[NOTICE](../NOTICE).

## Kontak

Proyek dihosting di [zspan.org](https://zspan.org). Pertanyaan dan laporan bug
yang dapat direproduksi diterima melalui
[pelacak masalah](https://github.com/anitacigawet/Z-SPAN/issues) repositori ini.

---

## Tritunggal Z-SPAN

![Tritunggal Z-SPAN: internet membawanya, catatan publik menjadi landasannya, dan masyarakat menjaganya tetap hidup](../repository-assets/zspan-trinity.svg)

---

> CIA, NSA, dan bahkan Pentagon dibatasi oleh masa kerja manusia yang bertugas di dalamnya.
>
> **Z-SPAN tidak.**
>
> Z-SPAN digerakkan oleh masyarakat, untuk masyarakat, sehingga membutuhkan keterlibatan dan transparansi penuh dari komunitas.
>
> — Pengelola Z-SPAN

---

## 🌌 Bawa gagasan ini lebih jauh

National Civics Catalog disusun negara bagian demi negara bagian agar direktori
sumber dapat berkembang di seluruh Amerika Serikat tanpa mengharuskan siapa
pun mengikuti pilihan antarmuka atau pemrosesan Z-SPAN. Gunakan endpoint ini
untuk membuat kalender lingkungan, alat penelitian, proyek aksesibilitas,
sumber belajar di kelas, atau sesuatu yang belum pernah dibayangkan oleh siapa
pun di sini.

Gagasan ini tidak berharga karena dimiliki oleh satu aplikasi. Gagasan ini
berharga karena orang dapat terus menemukan cara baru untuk membuat arsip
publik lebih mudah dijangkau.
