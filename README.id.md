[DRAF SEMENTARA YANG DITULIS OLEH AI. AKAN DITULIS ULANG PALING LAMBAT 4 AGUSTUS 2026]

# Z-SPAN

[English](README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [**Bahasa Indonesia**](README.id.md) · [Filipino](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**Perpustakaan virtual tentang politik lokal.**

[Kunjungi Z-SPAN di zspan.org](https://zspan.org)

✨ **Diterbitkan untuk ditelaah, dilestarikan, dan dijadikan inspirasi.**

Z-SPAN adalah upaya untuk membuat rapat publik pemerintah daerah lebih mudah
ditemukan, ditonton, dan dipahami. Wilayah ditampilkan sebagai kanal, rapat
sebagai episode, sementara video, agenda, dan notulen asli tetap menjadi bagian
dari jalur penelusuran.

Repositori ini adalah perpustakaan di balik perpustakaan: kumpulan terpilih
kode sumber publik, pola proyek, dan pelajaran yang mungkin berguna bagi siapa
saja yang sedang memikirkan proyek serupa di kota, negara bagian, atau negara
lain.

Repositori ini bukan salinan lengkap sistem produksi dan tidak dimaksudkan
untuk dikloning lalu diluncurkan sebagai instans Z-SPAN lain. Bagian yang berguna
di sini lebih kecil: ide navigasi, batas pemutaran yang jelas, cara menjaga
sumber asli tetap terlihat, atau prinsip desain yang dapat dibawa ke proyek
mandiri.

> Halaman ini adalah terjemahan README bahasa Inggris yang dibuat dengan
> bantuan AI. Koreksi melalui pull request dari penutur bahasa Indonesia yang
> fasih sangat dihargai. Jika ada perbedaan makna, [README bahasa Inggris](README.md),
> [LICENSE](LICENSE), dan [NOTICE](NOTICE) menjadi acuan. Dokumen lain yang
> ditautkan masih berbahasa Inggris.

---

## 📚 Mengapa perpustakaan ini ada

Proyek yang bekerja dengan arsip publik tingkat lokal cenderung menghadapi
pertanyaan yang sama:

- Bagaimana seseorang dapat menelusuri rapat ketika setiap situs pemerintah
  mengaturnya dengan cara berbeda?
- Bagaimana satu antarmuka dapat tetap berguna di berbagai kota dan penyedia
  video?
- Bagaimana jalur kembali ke sumber resmi dapat selalu terlihat jelas?
- Bagaimana sistem teknis dapat menjelaskan dirinya tanpa memaksa orang
  membaca basis data di baliknya?

Z-SPAN adalah salah satu jawaban yang sedang diterapkan, bukan satu-satunya.
Tujuan repositori ini adalah membuat gagasan yang berguna tetap cukup terlihat
untuk diperiksa, dipertanyakan, dan dikembangkan lebih jauh oleh proyek lain.

## 👋 Untuk siapa perpustakaan ini

Baik Anda pelajar, pegiat masyarakat, jurnalis, peneliti, desainer,
pengembang, relawan, maupun sekadar ingin tahu tentang informasi publik lokal,
Anda tidak perlu mengadopsi seluruh proyek untuk menemukan sesuatu yang
berguna di sini. Perpustakaan disusun agar setiap gagasan atau komponen dapat
dipahami satu per satu.

## 🧭 Cara menggunakan repositori ini

Tidak ada urutan baca yang wajib, tetapi berikut beberapa titik awal yang
berguna:

1. Baca [model proyek](docs/PROJECT_MODEL.md) untuk penjelasan paling sederhana
   tentang hubungan antarbagian.
2. Buka [katalog perpustakaan](CATALOG.md) untuk memilih bagian kode, prompt,
   atau desain berdasarkan pertanyaan yang ingin Anda telusuri.
3. Pelajari [pola yang dapat dibawa ke proyek lain](docs/DESIGN_PATTERNS.md)
   untuk memahami gagasan di balik antarmuka.
4. Gunakan [panduan repositori](docs/REPOSITORY_GUIDE.md) untuk mengikuti
   perjalanan pengunjung tertentu melalui kode yang dipublikasikan.
5. Periksa [apa yang dipublikasikan dan apa yang tidak](PUBLICATION_SCOPE.md)
   sebelum menarik kesimpulan tentang sistem Z-SPAN yang lebih luas.
6. Lihat [catatan snapshot saat ini](docs/snapshots/2026-08-02.md) untuk ukuran
   tepat dan status peninjauan rilis ini.

## 🗂️ Apa yang ada di dalam koleksi

Kode yang dipublikasikan saat ini menunjukkan enam bagian pengalaman
pengunjung:

- **Menemukan wilayah atau rapat** melalui tampilan beranda, kanal, kota, dan
  pencarian.
- **Menelusuri apa yang tersedia** melalui panduan yang dapat berpindah antara
  kartu, peta, pemutar tertanam, dan tampilan yang lebih besar.
- **Kembali ke arsip asli** melalui tautan yang jelas ke video, agenda, dan
  notulen resmi bila tersedia.
- **Memutar video melalui satu antarmuka bersama** meskipun penyedia video di
  belakangnya berbeda.
- **Menjelaskan pemeriksaan integritas kepada pengunjung** melalui tampilan
  audit, pemindaian, dan verifikasi.
- **Mengubah catatan rapat menjadi ringkasan urusan publik yang mudah dibaca**
  melalui tiga contoh yang telah ditinjau dan disimpan di bagian prompt.

[TAMPILAN VISUAL AKAN DITAMBAHKAN DI SINI]

[Panduan repositori](docs/REPOSITORY_GUIDE.md) menghubungkan setiap gagasan
tersebut dengan berkas yang berkaitan.

## Catatan tentang menjalankan kode

Anda tidak akan menemukan petunjuk instalasi, hosting, Docker, atau deployment
di repositori ini. Hal tersebut disengaja.

Berkas yang dipublikasikan dipilih dari sistem kerja privat yang lebih besar.
Sebagian modul yang diimpor, layanan, sambungan aplikasi, dan konfigurasi runtime tidak
disertakan. Kode sumber ini tersedia untuk dibaca dan dipelajari; bukan sebagai
aplikasi mandiri atau distribusi yang didukung.

## Susunan repositori

- [`docs/`](docs/) menjelaskan model proyek, pola yang dapat digunakan kembali,
  jalur baca, dan snapshot publik bertanggal.
- [`code/`](code/) memuat kode referensi antarmuka pengunjung yang dipilih,
  terpisah dari jalur proyek kerja privat.
- [`prompts/`](prompts/) memuat tiga contoh prompt yang telah ditinjau dan tidak
  diubah, yang dapat dipelajari atau disesuaikan satu per satu.
- [`CATALOG.md`](CATALOG.md) adalah indeks per bagian untuk pembaca manusia dan
  AI.
- [`PUBLICATION_SCOPE.md`](PUBLICATION_SCOPE.md) menjelaskan batas publikasi
  dengan bahasa yang jelas.

Ekspor publik hanya mengubah nama bagian. Struktur relatif di dalam
`code/visitor-interface/src/` dipertahankan agar hubungan antara halaman,
komponen, adaptor pemutar, dan gaya tetap mudah dibaca.

## ⚖️ Lisensi

Kode yang dipublikasikan tersedia di bawah
[PolyForm Noncommercial License 1.0.0](LICENSE). Kode dapat dipelajari,
disesuaikan, dibagikan, dan digunakan kembali untuk tujuan nonkomersial sesuai
dengan ketentuan lisensi. Ini mencakup pembelajaran pribadi, proyek hobi,
pendidikan, penelitian publik, kegiatan amal, dan penggunaan pemerintah.

Lisensi ini tidak memberikan izin untuk penggunaan komersial. Atribusi yang
wajib dan batas penggunaan nama Z-SPAN tercatat dalam [NOTICE](NOTICE).

## Kontak

Proyek dihosting di [zspan.org](https://zspan.org). Jika Anda tertarik mengisi
posisi yang masih terbuka dalam ekosistem Z-SPAN, hubungi
[anitacigawet@pm.me](mailto:anitacigawet@pm.me) untuk informasi lebih lanjut.
