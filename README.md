# File Organization Agent

Ứng dụng dòng lệnh cho Windows x64. Chọn một thư mục cá nhân, xem từng đề xuất kèm lý do, rồi duyệt kế hoạch trước khi di chuyển file. Mỗi lần thực thi có một mã phiên để xem lại và hoàn tác. Toàn bộ xử lý diễn ra trên máy.

## Cài trên Windows x64 với Python 3.13

```powershell
git clone https://github.com/HPHind110/file-organizer-agent.git C:\agent-projects\file-organizer-agent
cd C:\agent-projects\file-organizer-agent
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-test.txt
```

## HTTP API

Trong PowerShell tại thư mục dự án, tạo thư mục thử và chạy server.

```powershell
$folder = (New-Item -ItemType Directory -Path (Join-Path $env:TEMP ([guid]::NewGuid().ToString()))).FullName
$env:ORGANIZER_API_TOKEN = .\.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"
$folder
.\.venv\Scripts\python api_server.py --root "$folder"
```

Mở http://127.0.0.1:8101/docs để thử API. Để dùng nút Authorize, lấy giá trị biến ORGANIZER_API_TOKEN trong terminal riêng tư trước khi khởi động. /openapi.json cung cấp đầy đủ schema OpenAPI. Server chỉ bind loopback. Ngoại trừ health, các route dưới đây yêu cầu header Authorization: Bearer TOKEN.

| Route | JSON request | JSON response |
|---|---|---|
| GET /health | Không cần | 200, {"status":"ok"} |
| POST /plans | {"directory":"C:\\example","recursive":false}, directory phải đúng root của server | 201, Preview |
| GET /plans/{plan_id} | Không cần | 200, Preview |
| POST /plans/{plan_id}/execute | {"approved":true} duyệt toàn bộ dòng move | 200, Operation |
| POST /operations/{operation_id}/rollback | {"approved":true} | 200, Operation |

Preview gồm plan_id, root, duplicates (các nhóm trùng tên), items (source, status, reason, target và snapshot). Operation gồm operation_id và moves (seq, src, dst, state, detail). Kế hoạch rỗng trả operation_id null. Kiểm tra từng state để nhận biết thao tác chưa hoàn tất. Server giữ 32 plan gần nhất trong RAM, quét lại khi execute và từ chối plan cũ. Restart xóa cache plan. Lịch sử operation nằm trong thư mục đã chọn và hỗ trợ rollback sau restart.

401 báo lỗi xác thực, 403 sai thư mục, 404 ID không tồn tại, 409 thiếu đồng ý hoặc xung đột, 422 sai schema. CLI và API dùng chung engine. Cài riêng dependencies production bằng requirements-api.txt. Test HTTP dùng socket loopback thật và thư mục tạm.

## Thử trong một thư mục mới

Chạy các lệnh dưới đây trong thư mục dự án. Ví dụ tạo thư mục mới có tên ngẫu nhiên trong TEMP, rồi thêm hai file văn bản. Thư mục ví dụ vẫn còn sau khi chạy để mày kiểm tra kết quả.

```powershell
$folder = Join-Path $env:TEMP ("organizer-demo-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $folder | Out-Null
Set-Content -LiteralPath (Join-Path $folder "invoice-November.txt") -Value "Invoice 101"
Set-Content -LiteralPath (Join-Path $folder "lecture-notes.txt") -Value "Week 1"
.\.venv\Scripts\python.exe -B .\organize.py plan "$folder"
.\.venv\Scripts\python.exe -B .\organize.py apply "$folder"
```

Ở bước `apply`, đọc lại các dòng `MOVE` rồi nhập đúng **Plan ID** vừa hiển thị. Nhấn Enter để hủy. File đầu tiên sẽ đến `Organized\Finance`, file thứ hai đến `Organized\Study`. Cuối lượt chạy, CLI in **Run** và kết quả từng file. Plan ID dùng để duyệt một kế hoạch. Run ID dùng để tra cứu và hoàn tác một lần thực thi cụ thể.

```powershell
.\.venv\Scripts\python.exe -B .\organize.py history "$folder"
$runId = Read-Host 'Nhap Run ID vua hien thi'
.\.venv\Scripts\python.exe -B .\organize.py history "$folder" $runId
.\.venv\Scripts\python.exe -B .\organize.py rollback "$folder" $runId --dry-run
.\.venv\Scripts\python.exe -B .\organize.py rollback "$folder" $runId
```

Nhập `ROLLBACK <Run ID>` khi được hỏi. Mở lại thư mục ví dụ để kiểm tra hai file đã về đúng chỗ ban đầu.

## Dùng với dữ liệu thật

```powershell
$folder = Join-Path $HOME 'Downloads'
.\.venv\Scripts\python.exe -B .\organize.py apply "$folder" --recursive --dry-run
```

Mặc định chỉ xét file ngay trong thư mục đã chọn. Thêm `--recursive` để xét cả thư mục con. Hai lệnh `plan` và `apply --dry-run` chỉ đọc dữ liệu. Các lệnh này không tạo thư mục phân loại, cơ sở dữ liệu hay file kế hoạch. Có thể dùng `--json` để xem kích thước, thời gian sửa, ID file, SHA-256, đích đề xuất, lý do và các nhóm trùng tên.

| Kết quả xem trước | Ý nghĩa |
|---|---|
| `MOVE` | File có đích xác định và đủ điều kiện tại thời điểm quét. |
| `REVIEW` | Cần xử lý thủ công. Ví dụ hai file cùng nhắm tới một đích, đích đã tồn tại, dấu hiệu phân loại mâu thuẫn hoặc file đang bị khóa. |
| `SKIP` | Mục được bỏ qua theo quy tắc, chẳng hạn file ẩn, file hệ thống hoặc liên kết. |
| `DUPLICATE NAME` | Danh sách đường dẫn có cùng tên khi bỏ qua hoa thường. Hai file cùng tên có thể chứa nội dung khác nhau. |

Ứng dụng giữ nguyên tên file và gom theo dạng `Organized\<nhóm>\<tên gốc>`. Hai nguồn cùng nhắm một đích đều được đưa vào REVIEW. Agent không tự xóa bản trùng, ghi đè hoặc thêm hậu tố vào tên file. Muốn giải quyết xung đột, hãy xem hai đường dẫn, chủ động đổi tên hoặc sắp xếp file, rồi lập kế hoạch mới.

## Phân loại và cấu hình

Sửa `settings.json` trong thư mục dự án. `categories` ánh xạ phần mở rộng vào nhóm. Phần mở rộng ghép như `.tar.gz` được ưu tiên theo độ dài. `name_rules` áp dụng từ hoặc cụm từ trong tên file với các loại file được phép. So khớp bỏ qua hoa thường, dùng ranh giới từ và chuẩn hóa Unicode. Ví dụ `hóa_đơn.pdf` vào Finance, còn `myinvoices.pdf` theo nhóm mặc định của PDF. `recursive` thiết lập mặc định cho các lần chạy sau. Dùng `--config "C:\duong-dan\rules.json"` để đọc một bản cấu hình khác hoặc `--no-recursive` để tắt duyệt sâu cho một lần chạy.

Các nhóm mặc định gồm tài liệu, bảng tính, trình chiếu, ảnh, âm thanh, video, file nén, dữ liệu và mã nguồn. Quy tắc tên bổ sung ba nhóm Finance, Study và Screenshots. Agent nhận diện một số chữ ký đầu file PDF, PNG, JPEG, GIF và ZIP. Nếu đuôi file mâu thuẫn với chữ ký đã nhận diện, hoặc tên khớp nhiều nhóm, agent yêu cầu xem lại. File thiếu đuôi có thể được phân loại nếu chữ ký xác định được một nhóm duy nhất. Với định dạng thiếu chữ ký đã hỗ trợ, quyết định có thể chỉ dựa trên tên và đuôi. Agent không phân tích nội dung văn bản, không OCR và không dùng LLM.

## Duyệt cùng một kế hoạch trong script

```powershell
$preview = .\.venv\Scripts\python.exe -B .\organize.py plan "$folder" --recursive --json | ConvertFrom-Json
$preview.items | Format-Table status, source, target, reason -AutoSize
```

Sau khi đọc bảng và quyết định thực hiện, chạy lệnh sau trong cùng phiên PowerShell.

```powershell
.\.venv\Scripts\python.exe -B .\organize.py apply "$folder" --recursive --approve $preview.plan_id
```

Agent quét lại với cùng tùy chọn. Nếu kế hoạch khác mã đã duyệt, nó dừng trước khi tạo lịch sử hoặc di chuyển file. Trong lúc thực thi, agent tiếp tục kiểm tra riêng từng nguồn và đích. Một file gặp lỗi có thể được bỏ qua trong khi các file còn lại hoàn tất. Khi cần hoàn tác một phiên đã chọn trong script, dùng `rollback "$folder" $runId --yes`.

| Mã thoát | Ý nghĩa |
|---|---|
| 0 | Xem trước hoặc tra cứu thành công, hoặc thực thi đã hoàn tất. Hãy đọc mục REVIEW trong kết quả xem trước. |
| 1 | Lỗi lệnh lúc chạy, hủy xác nhận, sai mã duyệt hoặc thiếu quyền. |
| 2 | Apply còn mục REVIEW hoặc thao tác chưa hoàn tất, rollback còn mục chưa xử lý, hoặc cú pháp CLI sai (argparse). |
| 130 | Người dùng ngắt tiến trình. |

## Lịch sử, tính an toàn và phục hồi

- Ứng dụng chỉ di chuyển trong thư mục dữ liệu đã chọn. Nó bỏ qua cây đầu ra `Organized` ở cấp gốc, thư mục trạng thái, bản cài đặt của chính nó, mục bắt đầu bằng dấu chấm hoặc `~$`, mục ẩn, mục hệ thống và reparse point, kể cả junction. Nếu thư mục gốc đi qua liên kết, ứng dụng từ chối.
- Trong lúc chuyển, ứng dụng giữ handle của file để chặn tiến trình khác ghi hoặc đổi tên. Nó đối chiếu ổ đĩa, ID file, kích thước, thời gian sửa và SHA-256 của luồng dữ liệu chính, rồi yêu cầu Windows đổi tên với cờ cấm thay thế. Sau đó nó kiểm tra danh tính và kích thước ở đích. Nếu nguồn biến mất, bị khóa hoặc bị chỉnh sửa, chi tiết lỗi nằm trong báo cáo.
- Lịch sử nằm trong `.file-organizer\history.sqlite3` ngay dưới thư mục dữ liệu đã chọn. Thư mục này còn chứa dấu nhận dạng `owner`. Giữ nguyên cả hai để hoàn tác. Các lần thực thi trên cùng gốc được khóa để tránh ghi đồng thời. Lịch sử lưu đường dẫn, trạng thái, lỗi và dấu vân tay của thao tác đã duyệt. Các mục chỉ xuất hiện khi quét có trong kết quả `plan --json`.
- Nếu bị ngắt, dùng `history` để tìm Run ID rồi chạy `rollback`. Trạng thái `planned` là thao tác chưa bắt đầu. Với `moving` hoặc `undoing`, agent kiểm tra vị trí và dấu vân tay thực tế trước khi quyết định. Các thao tác đã xong có thể được hoàn tác theo thứ tự ngược. Nếu file đã thay đổi, đường dẫn gốc bị chiếm hoặc thư mục cha gốc đã mất, agent giữ dữ liệu hiện có và báo lý do. Có thể đóng chương trình đang khóa file, giải quyết xung đột hoặc tạo lại thư mục cha rồi thử rollback cùng Run ID.
- Rollback chỉ khôi phục các lần di chuyển có trong lịch sử. Nó không khôi phục phiên bản nội dung đã sửa, file người dùng đã xóa hoặc file từ ổ bị hỏng. Thư mục phân loại rỗng và lịch sử được giữ lại.

## Phạm vi đã kiểm tra

Thiết kế cho thư mục dữ liệu cá nhân trên ổ cục bộ Windows. Gói runtime dành cho x64. Đường dẫn UNC, ổ mạng và gốc cả ổ đĩa bị từ chối. Dùng thư mục dữ liệu tách khỏi thư mục ứng dụng. Khi bật quét sâu, mọi file thường có thể được đưa ra khỏi cấu trúc thư mục gốc, kể cả mã nguồn hoặc tài nguyên ứng dụng. Hãy chọn đúng cây thư mục muốn tổ chức.

Mỗi lần lập kế hoạch đọc toàn bộ luồng dữ liệu chính để tính hash, nên thời gian phụ thuộc tổng dung lượng. Agent không so sánh luồng NTFS phụ, quyền ACL hay tag ảnh. Thiết kế giả định tài khoản cục bộ kiểm soát thư mục và nhật ký. Nếu mất lịch sử thì phải sắp xếp lại thủ công.

## Mã nguồn và kiểm thử

| Thành phần | Vai trò |
|---|---|
| `organize.py`, `organizer/cli.py` | Điểm vào, xem trước, xác nhận, báo cáo và đầu ra JSON |
| `organizer/planner.py` | Kiểm tra cấu hình, nhận diện loại file, lập kế hoạch và phát hiện xung đột |
| `organizer/windows.py` | Kiểm tra đường dẫn, handle Windows, SHA-256 và đổi tên nguyên tử cho từng file |
| `organizer/engine.py` | Thực thi, xác minh, nhật ký SQLite và rollback |
| `settings.json` | Cấu hình mặc định |
| `tests/test_organizer.py` | Kiểm thử tự động bằng thư mục tạm, gồm chạy qua CLI và ngắt tiến trình thật |
| `TEST-RESULTS.txt` | Lệnh chạy, môi trường và kết quả thực tế của bản bàn giao |

```powershell
Set-Location -LiteralPath 'C:\agent-projects\file-organizer-agent'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Test tạo junction tạm sẽ tự bỏ qua nếu Windows từ chối quyền tạo. Một test riêng kiểm tra junction tương thích có sẵn trên Windows bằng thao tác chỉ đọc.
