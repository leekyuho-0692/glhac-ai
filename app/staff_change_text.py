"""KO/EN/ID catalogue for staff change notifications (values remain user content)."""
import json


COPY = {
    "ko": {"title": "[{company}] 담당자 변경 내역 {count}건", "deleted": "삭제",
           "empty": "없음", "staff": "직원", "field": "추가 항목", "yes": "예", "no": "아니요",
           "fallback": "업체 계정이 없어 담당 컨설턴트에게 변경 내역을 보냅니다."},
    "en": {"title": "[{company}] {count} staff changes", "deleted": "Deleted",
           "empty": "None", "staff": "Staff", "field": "Additional field", "yes": "Yes", "no": "No",
           "fallback": "Sent to the assigned consultant because the company has no applicant account."},
    "id": {"title": "[{company}] {count} perubahan oleh petugas", "deleted": "Dihapus",
           "empty": "Tidak ada", "staff": "Petugas", "field": "Kolom tambahan", "yes": "Ya", "no": "Tidak",
           "fallback": "Dikirim kepada konsultan yang ditunjuk karena perusahaan tidak memiliki akun pemohon."},
}
ROLES = {
    "consultant": ("컨설턴트", "Consultant", "Konsultan"),
    "operator": ("운영자", "Operator", "Operator"),
    "admin": ("관리자", "Administrator", "Administrator"),
    "auditor": ("심사자", "Auditor", "Auditor"),
    "penyelia_halal": ("할랄 감독자", "Halal supervisor", "Penyelia halal"),
    "pendamping_pph": ("할랄 지원 담당자", "Halal assistant", "Pendamping PPH"),
    "fatwa_liaison": ("파트와 담당자", "Fatwa liaison", "Petugas fatwa"),
    "applicant": ("업체 담당자", "Applicant", "Pemohon"),
}
RESOURCES = {
    "case_application": ("신청 건", "Application", "Permohonan"),
    "document_asset": ("서류", "Document", "Dokumen"),
    "material": ("원재료", "Material", "Bahan baku"),
    "product": ("제품", "Product", "Produk"),
    "product_material": ("제품·원재료 연결", "Product/material link", "Hubungan produk/bahan"),
    "material_evidence_links": ("원재료 증빙 연결", "Material evidence link", "Hubungan bukti bahan"),
    "material_measurement": ("원재료 측정값", "Material measurement", "Pengukuran bahan"),
    "certificate_scope_item": ("증빙 인증 범위", "Evidence certificate scope", "Lingkup sertifikat bukti"),
    "sjph_evidence": ("SJPH 증빙", "SJPH evidence", "Bukti SJPH"),
    "facility": ("공장·시설", "Facility", "Fasilitas"),
    "vehicle": ("운송 차량", "Vehicle", "Kendaraan"),
    "vehicle_cleaning": ("차량 세척 기록", "Vehicle cleaning", "Catatan pembersihan kendaraan"),
    "penyelia_halal": ("할랄 감독자 정보", "Halal supervisor profile", "Profil penyelia halal"),
    "org": ("업체 정보", "Company profile", "Profil perusahaan"),
    "app_user": ("업체 계정", "Applicant account", "Akun pemohon"),
}
FIELDS = {
    "name": ("이름", "Name", "Nama"), "company_name": ("업체명", "Company name", "Nama perusahaan"),
    "address": ("주소", "Address", "Alamat"), "nib": ("사업자등록번호", "Business number", "NIB"),
    "responsible_person": ("책임자", "Responsible person", "Penanggung jawab"),
    "halal_supervisor": ("할랄 감독자", "Halal supervisor", "Penyelia halal"),
    "email": ("이메일", "Email", "Email"), "phone": ("전화번호", "Phone", "Telepon"),
    "factory_reg_no": ("공장등록번호", "Factory registration number", "Nomor registrasi pabrik"),
    "factory_address": ("공장 주소", "Factory address", "Alamat pabrik"),
    "due_date": ("목표 기한", "Due date", "Tenggat"),
    "notify_consent": ("알림 수신 동의", "Notification consent", "Persetujuan notifikasi"),
    "status": ("상태", "Status", "Status"), "draft_state": ("신청 단계", "Application stage", "Tahap permohonan"),
    "return_reason": ("반려 사유", "Return reason", "Alasan pengembalian"),
    "pathway": ("인증 경로", "Certification pathway", "Jalur sertifikasi"),
    "scheme": ("인증 종류", "Certification scheme", "Skema sertifikasi"),
    "logistics_scope": ("물류 서비스 범위", "Logistics scope", "Lingkup logistik"),
    "scheme_frozen": ("인증 종류 잠금", "Scheme lock", "Penguncian skema"),
    "scope_frozen": ("인증 범위 잠금", "Scope lock", "Penguncian lingkup"),
    "risk_category": ("위험 분류", "Risk category", "Kategori risiko"),
    "is_msme": ("중소기업 여부", "MSME", "UMKM"),
    "annual_revenue": ("연매출", "Annual revenue", "Pendapatan tahunan"),
    "outlet_count": ("영업장 수", "Outlet count", "Jumlah gerai"),
    "sehati_eligible": ("자기선언 적격 여부", "Self-declaration eligibility", "Kelayakan pernyataan mandiri"),
    "fatwa_status": ("파트와 상태", "Fatwa status", "Status fatwa"),
    "facility_ids": ("선택 시설", "Selected facilities", "Fasilitas terpilih"),
    "product_ids": ("선택 제품", "Selected products", "Produk terpilih"),
    "profile_ext": ("상세 정보", "Profile", "Profil"), "fields": ("서류 항목", "Document fields", "Kolom dokumen"),
    "filename": ("파일명", "Filename", "Nama berkas"), "filename_en": ("영문 파일명", "English filename", "Nama berkas Inggris"),
    "doc_type": ("서류 종류", "Document type", "Jenis dokumen"),
    "review_status": ("검토 상태", "Review status", "Status peninjauan"),
    "content_type": ("파일 형식", "File type", "Jenis berkas"),
    "material_id": ("연결 원재료", "Linked material", "Bahan terkait"),
    "product_id": ("연결 제품", "Linked product", "Produk terkait"),
    "document_id": ("연결 서류", "Linked document", "Dokumen terkait"),
    "org_id": ("소속 업체", "Company", "Perusahaan"), "case_id": ("신청 건", "Application", "Permohonan"),
    "lat": ("위도", "Latitude", "Lintang"), "lng": ("경도", "Longitude", "Bujur"),
    "geo_source": ("위치 출처", "Location source", "Sumber lokasi"),
    "captured_at": ("촬영 시각", "Capture time", "Waktu pengambilan"),
    "category": ("분류", "Category", "Kategori"), "description": ("설명", "Description", "Deskripsi"),
    "registration_type": ("등록 유형", "Registration type", "Jenis pendaftaran"),
    "e_number": ("식품첨가물 번호", "E number", "Nomor E"), "mat_type": ("원재료 유형", "Material type", "Jenis bahan"),
    "source": ("유래", "Source", "Sumber"), "supplier": ("공급사", "Supplier", "Pemasok"),
    "manufacturer": ("제조사", "Manufacturer", "Produsen"), "origin": ("원산지", "Origin", "Asal"),
    "cert": ("인증 여부", "Certification", "Sertifikasi"), "cert_no": ("인증번호", "Certificate number", "Nomor sertifikat"),
    "note": ("비고", "Note", "Catatan"), "evidence_provided": ("증빙 제출 여부", "Evidence provided", "Bukti tersedia"),
    "source_known": ("유래 확인 여부", "Source known", "Sumber diketahui"),
    "link_type": ("증빙 연결 유형", "Evidence link type", "Jenis hubungan bukti"),
    "link_reason": ("증빙 연결 사유", "Evidence link reason", "Alasan hubungan bukti"),
    "match_basis": ("연결 근거", "Match basis", "Dasar pencocokan"), "page": ("쪽", "Page", "Halaman"),
    "param_key": ("시험 항목", "Test parameter", "Parameter uji"), "value": ("측정값", "Value", "Nilai"),
    "unit": ("단위", "Unit", "Satuan"), "method": ("방법", "Method", "Metode"),
    "lab_name": ("시험기관", "Laboratory", "Laboratorium"), "tested_at": ("시험일", "Test date", "Tanggal uji"),
    "certificate_no": ("인증번호", "Certificate number", "Nomor sertifikat"),
    "scope_type": ("범위 종류", "Scope type", "Jenis lingkup"), "scope_value": ("인증 대상", "Certified subject", "Objek bersertifikat"),
    "issuer": ("발급기관", "Issuer", "Penerbit"), "issue_date": ("발급일", "Issue date", "Tanggal terbit"),
    "expiry_date": ("만료일", "Expiry date", "Tanggal kedaluwarsa"), "source_page": ("근거 쪽", "Source page", "Halaman sumber"),
    "item_key": ("증빙 항목", "Evidence item", "Butir bukti"),
    "city": ("도시", "City", "Kota"), "country": ("국가", "Country", "Negara"),
    "province": ("주·도", "Province", "Provinsi"), "zip": ("우편번호", "Postal code", "Kode pos"),
    "reg_no": ("등록번호", "Registration number", "Nomor registrasi"),
    "plate_no": ("차량번호", "Plate number", "Nomor kendaraan"),
    "vehicle_type": ("차량 종류", "Vehicle type", "Jenis kendaraan"),
    "transport_type": ("운반 형태", "Transport type", "Jenis pengangkutan"),
    "capacity": ("용량", "Capacity", "Kapasitas"), "previous_cargo": ("직전 화물", "Previous cargo", "Muatan sebelumnya"),
    "previous_cargo_halal": ("직전 화물 할랄 여부", "Previous cargo halal status", "Status halal muatan sebelumnya"),
    "last_cleaned": ("최근 세척일", "Last cleaned", "Pembersihan terakhir"),
    "sertu": ("정결 세정 여부", "Sertu performed", "Sertu dilakukan"),
    "vehicle_id": ("차량", "Vehicle", "Kendaraan"), "cleaned_at": ("세척 일시", "Cleaning time", "Waktu pembersihan"),
    "sertu_steps": ("정결 세정 횟수", "Sertu steps", "Tahapan sertu"),
    "photo_doc_id": ("세척 사진", "Cleaning photo", "Foto pembersihan"),
    "penyelia_id": ("할랄 감독자", "Halal supervisor", "Penyelia halal"),
    "penyelia_sign": ("감독자 서명 여부", "Supervisor signed", "Tanda tangan penyelia"),
    "signed_at": ("서명 시각", "Signature time", "Waktu tanda tangan"),
    "next_due": ("다음 세척 예정일", "Next cleaning due", "Jadwal pembersihan berikutnya"),
    "user_id": ("연결 계정", "Linked account", "Akun terkait"),
    "training_cert": ("교육 수료증", "Training certificate", "Sertifikat pelatihan"),
    "cert_expiry": ("수료증 만료일", "Training certificate expiry", "Masa berlaku sertifikat pelatihan"),
    "username": ("계정 아이디", "Account username", "Nama pengguna"), "role": ("역할", "Role", "Peran"),
    "company_role": ("업체 내 역할", "Company role", "Peran perusahaan"),
    "contact_email": ("연락 이메일", "Contact email", "Email kontak"),
    "contact_phone": ("연락 전화번호", "Contact phone", "Telepon kontak"),
    "display_name": ("표시 이름", "Display name", "Nama tampilan"),
    "office_phone": ("사무실 전화", "Office phone", "Telepon kantor"),
    "total_employee": ("직원 수", "Employee count", "Jumlah karyawan"),
    "employee_count": ("직원 수", "Employee count", "Jumlah karyawan"),
    "sector": ("업종", "Business sector", "Sektor usaha"), "sectors": ("업종", "Business sectors", "Sektor usaha"),
    "product_names": ("제품명 목록", "Product names", "Nama produk"),
    "material_names": ("원재료명 목록", "Material names", "Nama bahan"),
    "product_aliases": ("제품 별칭", "Product aliases", "Alias produk"),
    "lang": ("언어", "Language", "Bahasa"), "language": ("언어", "Language", "Bahasa"),
    "locale": ("언어", "Language", "Bahasa"),
    "business_type": ("사업 유형", "Business type", "Jenis usaha"),
    "establishment_date": ("설립일", "Establishment date", "Tanggal pendirian"),
    "corporate_reg_no": ("법인등록번호", "Corporate registration number", "Nomor registrasi badan usaha"),
    "company_name_ko": ("한글 업체명", "Korean company name", "Nama perusahaan Korea"),
    "company_name_en": ("영문 업체명", "English company name", "Nama perusahaan Inggris"),
    "responsible_person_en": ("영문 책임자명", "Responsible person's English name", "Nama penanggung jawab Inggris"),
}


def translated(catalogue, key, lang, default):
    return catalogue[key][("ko", "en", "id").index(lang)] if key in catalogue else default


def field_label(key, lang):
    return " / ".join(translated(FIELDS, part, lang,
        COPY[lang]["field"] + " (" + part.replace("_", " ") + ")") for part in key.split("."))


def render(params, lang="ko"):
    lang = lang if lang in COPY else "ko"
    copy = COPY[lang]
    entries = params.get("entries", [])
    count = sum(1 if row["op"] == "delete" else len(row["changes"]) for row in entries)
    title = copy["title"].format(company=params.get("company") or "—", count=count)
    lines = [copy["fallback"]] if params.get("fallback") else []

    def value(item):
        if item is None or item == "":
            return copy["empty"]
        if isinstance(item, bool):
            return copy["yes" if item else "no"]
        return json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item)

    for row in entries:
        resource = translated(RESOURCES, row["resource_type"], lang, row["resource_type"])
        lines.append("\n" + resource + " · " + (row.get("label") or row["resource_id"]))
        for actor in row["actors"]:
            role = translated(ROLES, actor["role"], lang, copy["staff"])
            lines.append(" · ".join(filter(None, (role, actor.get("name") or copy["staff"], actor["at"] + " UTC"))))
        if row["op"] == "delete":
            lines.append(copy["deleted"] + ": " + (row.get("label") or row["resource_id"]))
        else:
            for key, change in row["changes"].items():
                lines.append(field_label(key, lang) + ": " + value(change["before"]) + " → " + value(change["after"]))
    return title, "\n".join(lines).strip()
