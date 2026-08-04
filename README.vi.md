[BẢN THẢO TẠM THỜI DO AI SOẠN. SẼ ĐƯỢC VIẾT LẠI CHẬM NHẤT VÀO NGÀY 4 THÁNG 8 NĂM 2026]

# Z-SPAN

[English](README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [Filipino](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [**Tiếng Việt**](README.vi.md)

**Một thư viện ảo về chính trị địa phương.**

[Truy cập Z-SPAN tại zspan.org](https://zspan.org)

✨ **Được công bố để mọi người xem xét, lưu giữ và tìm cảm hứng.**

Z-SPAN là một nỗ lực giúp các cuộc họp công khai của chính quyền địa phương dễ
tìm, dễ xem và dễ hiểu hơn. Mỗi địa phương trở thành một kênh, mỗi cuộc họp
trở thành một tập, còn video, chương trình nghị sự và biên bản gốc
luôn nằm trong đường dẫn tra cứu.

Kho mã này là thư viện phía sau thư viện: một tuyển tập mã nguồn công khai,
phương pháp xây dựng dự án và những bài học có thể hữu ích cho bất kỳ ai đang
nghĩ đến một dự án tương tự ở thành phố, tiểu bang hoặc quốc gia khác.

Đây không phải là bản sao đầy đủ của hệ thống đang vận hành và không nhằm để
sao chép rồi triển khai thành một bản triển khai Z-SPAN khác. Điều hữu ích ở đây nhỏ
hơn: một ý tưởng điều hướng, một ranh giới rõ ràng cho việc phát video, một
cách giữ tài liệu nguồn luôn hiện hữu, hoặc một nguyên tắc thiết kế có thể được
đưa vào một dự án độc lập.

> Trang này là bản dịch README tiếng Anh có sự hỗ trợ của AI. Chúng tôi hoan
> nghênh người thông thạo tiếng Việt gửi chỉnh sửa qua pull request. Nếu có
> khác biệt về ý nghĩa, hãy lấy [README tiếng Anh](README.md) làm bản chuẩn;
> [LICENSE](LICENSE) là văn bản chuẩn về điều khoản cấp phép, còn
> [NOTICE](NOTICE) là văn bản chuẩn về ghi công bắt buộc và giới hạn sử dụng
> tên Z-SPAN. Các tài liệu liên kết khác hiện vẫn bằng tiếng Anh.

---

## 📚 Vì sao thư viện này tồn tại

Các dự án làm việc với hồ sơ công khai địa phương thường gặp những câu hỏi
giống nhau:

- Một người nên tìm các cuộc họp như thế nào khi mỗi trang web chính quyền sắp
  xếp chúng theo một cách khác nhau?
- Làm sao một giao diện vẫn hữu ích giữa nhiều thành phố và nền tảng video?
- Làm sao để đường dẫn trở lại nguồn chính thức luôn rõ ràng?
- Làm sao hệ thống kỹ thuật có thể tự giải thích mà không bắt mọi người phải
  đọc cơ sở dữ liệu bên dưới?

Z-SPAN là một câu trả lời đang được áp dụng, không phải câu trả lời duy nhất.
Mục tiêu của kho mã này là giữ những ý tưởng hữu ích đủ rõ để người khác có thể
xem xét, đặt câu hỏi và phát triển chúng trong những dự án khác.

## 👋 Thư viện này dành cho ai

Dù bạn là học sinh, nhà hoạt động xã hội, nhà báo, nhà nghiên cứu, nhà thiết
kế, lập trình viên, tình nguyện viên hay chỉ đơn giản tò mò về thông tin công
khai địa phương, bạn không cần áp dụng toàn bộ dự án mới tìm thấy điều hữu ích
ở đây. Thư viện được sắp xếp để mỗi lần có thể hiểu một ý tưởng hoặc một thành
phần.

## 🧭 Cách sử dụng kho mã này

Không có thứ tự đọc bắt buộc, nhưng đây là những điểm bắt đầu hữu ích:

1. Đọc [mô hình dự án](docs/PROJECT_MODEL.md) để có lời giải thích đơn giản
   nhất về mối liên hệ giữa các phần.
2. Mở [danh mục thư viện](CATALOG.md) để chọn khu vực mã, prompt hoặc thiết kế
   theo câu hỏi bạn muốn tìm hiểu.
3. Xem [những mẫu có thể áp dụng ở nơi khác](docs/DESIGN_PATTERNS.md) để hiểu
   các ý tưởng phía sau giao diện.
4. Dùng [hướng dẫn kho mã](docs/REPOSITORY_GUIDE.md) để đi theo một hành trình
   cụ thể của khách truy cập trong phần mã đã công bố.
5. Kiểm tra [nội dung nào được công bố và nội dung nào không](PUBLICATION_SCOPE.md)
   trước khi kết luận về hệ thống Z-SPAN rộng hơn.
6. Xem [bản ghi snapshot hiện tại](docs/snapshots/2026-08-02.md) để biết quy mô
   chính xác và trạng thái rà soát của lần công bố này.

## 🗂️ Những gì có trong bộ sưu tập

Mã nguồn công bố hiện minh họa sáu phần của trải nghiệm khách truy cập:

- **Tìm một địa phương hoặc cuộc họp** qua trang chủ, kênh, thành phố và tìm
  kiếm.
- **Duyệt những gì đang có** qua một hướng dẫn có thể chuyển giữa thẻ, bản đồ,
  trình phát nhúng và chế độ xem lớn hơn.
- **Trở lại hồ sơ gốc** qua các liên kết rõ ràng đến video, chương trình nghị
  sự và biên bản chính thức khi có sẵn.
- **Phát video qua một giao diện chung** ngay cả khi nền tảng lưu trữ phía sau
  thay đổi.
- **Giải thích việc kiểm tra tính toàn vẹn cho khách truy cập** qua các trang
  kiểm toán, quét và xác minh.
- **Chuyển hồ sơ cuộc họp thành bản tóm lược dễ đọc về các vấn đề công cộng** qua
  ba ví dụ đã được rà soát trong khu vực prompt.

[PHẦN TRÌNH BÀY HÌNH ẢNH SẼ ĐƯỢC THÊM TẠI ĐÂY]

[Hướng dẫn kho mã](docs/REPOSITORY_GUIDE.md) liên kết từng ý tưởng này với các
tệp tương ứng.

## Lưu ý về việc chạy mã

Bạn sẽ không tìm thấy hướng dẫn cài đặt, lưu trữ, Docker hoặc triển khai trong
kho mã này. Điều đó là có chủ ý.

Các tệp được công bố được chọn từ một hệ thống làm việc riêng tư lớn hơn. Một
số mô-đun được import, dịch vụ, kết nối ứng dụng và cấu hình chạy không được đưa vào.
Mã nguồn ở đây để đọc và nghiên cứu; nó không được giới thiệu như một ứng dụng
độc lập hoặc một bản phân phối được hỗ trợ.

## Cách tổ chức kho mã

- [`docs/`](docs/) giải thích mô hình dự án, các mẫu có thể tái sử dụng, lộ
  trình đọc và các snapshot công khai có ghi ngày.
- [`code/`](code/) chứa mã tham khảo đã chọn của giao diện khách truy cập, được
  sắp xếp tách khỏi đường dẫn dự án làm việc riêng tư.
- [`prompts/`](prompts/) chứa ba ví dụ prompt đã được rà soát và giữ nguyên,
  có thể được nghiên cứu hoặc điều chỉnh riêng lẻ.
- [`CATALOG.md`](CATALOG.md) là chỉ mục theo từng khu vực cho người đọc và AI.
- [`PUBLICATION_SCOPE.md`](PUBLICATION_SCOPE.md) nêu rõ ranh giới công bố bằng
  ngôn ngữ dễ hiểu.

Bản xuất công khai chỉ đổi tên các khu vực. Cấu trúc tương đối bên trong
`code/visitor-interface/src/` được giữ nguyên để mối quan hệ giữa trang, thành
phần, bộ chuyển đổi trình phát và kiểu dáng vẫn dễ đọc.

## ⚖️ Giấy phép

Mã đã công bố được cung cấp theo
[PolyForm Noncommercial License 1.0.0](LICENSE). Mã có thể được nghiên cứu,
điều chỉnh, chia sẻ và tái sử dụng cho mục đích phi thương mại theo các điều
khoản của giấy phép. Điều này bao gồm học tập cá nhân, dự án sở thích, giáo
dục, nghiên cứu công, hoạt động từ thiện và sử dụng của chính quyền.

Giấy phép này không cho phép sử dụng thương mại. Yêu cầu ghi nguồn và giới hạn
sử dụng tên Z-SPAN được ghi trong [NOTICE](NOTICE).

## Liên hệ

Dự án được lưu trữ tại [zspan.org](https://zspan.org). Nếu bạn quan tâm đến một
vị trí đang mở trong hệ sinh thái Z-SPAN, hãy liên hệ
[anitacigawet@pm.me](mailto:anitacigawet@pm.me) để biết thêm thông tin.
