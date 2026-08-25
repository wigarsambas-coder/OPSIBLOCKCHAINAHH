import streamlit as st

from forensics_core import SecureBlockchain, register_image_bytes, verify_image_bytes

st.set_page_config(page_title="OPSI — Verifikasi Keaslian Citra", layout="wide")

BUCKET_NAME = "gridproof"  
LEDGER_BLOB_PATH = "ledger/opsi_ledger.json"
ADMIN_EMAILS = list(st.secrets.get("admin_emails", []))


@st.cache_resource
def get_blockchain():
    creds_info = dict(st.secrets["gcp_service_account"])
    return SecureBlockchain(bucket_name=BUCKET_NAME, blob_path=LEDGER_BLOB_PATH, credentials_info=creds_info)

try:
    blockchain = get_blockchain()
except Exception as e:
    st.error("❌ Gagal terhubung ke Google Cloud Storage.")
    st.code(str(e))
    st.warning(
        "Aplikasi di stop. "
        "Cek status billing/izin GCS dulu, baru reload halaman ini."
    )
    st.stop()

#Admin stuff
is_admin = st.user.is_logged_in and st.user.email in ADMIN_EMAILS

with st.sidebar:
    if st.user.is_logged_in:
        if is_admin:
            st.success(f"🛡️ Admin: {st.user.email}")
        else:
            st.warning(f"Login sebagai {st.user.email}\n\n(bukan akun admin, akses tetap terbatas ke Verifikasi)")
        st.button("Log out", on_click=st.logout)
    else:
        st.button("Login sebagai Admin", on_click=st.login)

st.title("Sistem Verifikasi Keaslian Citra - Gridproof")
jumlah_aktif = sum(1 for b in blockchain.chain[1:] if not b.metadata.get("deleted"))
st.caption(f"Ledger saat ini: **{jumlah_aktif}** citra asli terdaftar (aktif).")

# Tab Registrasi & Kelola Ledger 
if is_admin:
    tab_daftar, tab_verifikasi, tab_kelola = st.tabs(
        ["📝 Registrasi Gambar Asli", "🔍 Verifikasi Gambar", "🛡️ Kelola Ledger Blockchain"]
    )
else:
    (tab_verifikasi,) = st.tabs(["🔍 Verifikasi Gambar Suspek"])

# =========================================================================
# TAB REGISTRASI (admin only) — upload manual ATAU tarik dari Kaggle
# =========================================================================
if is_admin:
    with tab_daftar:
        st.subheader("Daftarkan gambar asli ke blockchain")
        sumber = st.radio(
            "Sumber gambar",
            ["Upload manual", "Upload ZIP (folder terkompresi)", "Dari Kaggle"],
            horizontal=True,
        )

        if sumber == "Upload manual":
            files_asli = st.file_uploader(
                "Upload satu atau beberapa gambar asli",
                type=["jpg", "jpeg", "png", "webp"],
                accept_multiple_files=True,
                key="upload_asli",
            )
            if st.button("Daftarkan ke Blockchain", disabled=not files_asli):
                for f in files_asli:
                    hasil = register_image_bytes(blockchain, f.read(), f.name)
                    if hasil["status"] == "terdaftar":
                        st.success(f"✅ {hasil['filename']} — terdaftar (Block #{hasil['block_index']})")
                    elif hasil["status"] == "duplikat":
                        st.warning(f"⚠️ {hasil['filename']} — sudah terdaftar sebelumnya sebagai '{hasil['file_asli']}'")
                    elif hasil["status"] == "gagal_simpan":
                        st.error(f"🛑 {hasil['filename']} — GAGAL disimpan ke GCS (data BELUM terdaftar): {hasil['error']}")
                    else:
                        st.error(f"❌ {hasil['filename']} — gagal dibaca, cek format filenya")

        elif sumber == "Upload ZIP":
            st.caption(
                "Upload satu file .zip "
                "semua gambar di dalamnya dicari otomatis"
            )
            zip_file = st.file_uploader("Upload file .zip", type=["zip"], key="upload_zip")
            filter_substring = st.text_input(
                "Filter nama file (opsional)",
                value="",
                placeholder="mis. ketik _Original untuk cuma proses file yang namanya mengandung itu",
                help=(
                    "Kosongkan untuk proses SEMUA gambar di dalam zip. Isi kalau zip-nya berisi "
                    "campuran citra asli + hasil edit (mis. dari notebook batch-edit robustness test) "
                    "— supaya yang terdaftar ke ledger cuma citra asli, bukan versi editannya."
                ),
            )
            batas_gambar_zip = st.number_input(
                "Maksimal gambar yang diproses per klik", min_value=1, max_value=2000, value=200, key="batas_zip"
            )

            if st.button("Ekstrak & Daftarkan dari ZIP", disabled=not zip_file):
                import os
                import zipfile
                import tempfile

                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        with st.spinner("Mengekstrak ZIP..."):
                            with zipfile.ZipFile(zip_file) as zf:
                                zf.extractall(tmpdir)

                        valid_ext = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
                        daftar_file = []
                        for root, _, files_ in os.walk(tmpdir):
                            for fn in files_:
                                if not fn.lower().endswith(valid_ext):
                                    continue
                                if filter_substring and filter_substring.lower() not in fn.lower():
                                    continue
                                daftar_file.append(os.path.join(root, fn))
                        daftar_file = sorted(daftar_file)[: int(batas_gambar_zip)]

                        if not daftar_file:
                            st.warning("Gak ada gambar yang cocok ditemukan di dalam zip (cek lagi filter nama-nya).")

                        st.write(f"Ditemukan {len(daftar_file)} gambar yang cocok, mendaftarkan...")
                        progress = st.progress(0)
                        for i, path in enumerate(daftar_file):
                            with open(path, "rb") as fh:
                                file_bytes = fh.read()
                            hasil = register_image_bytes(
                                blockchain, file_bytes, os.path.basename(path), source_label="zip_upload"
                            )
                            if hasil["status"] == "terdaftar":
                                st.success(f"✅ {hasil['filename']} — Block #{hasil['block_index']}")
                            elif hasil["status"] == "duplikat":
                                st.warning(f"⚠️ {hasil['filename']} — duplikat dari '{hasil['file_asli']}'")
                            elif hasil["status"] == "gagal_simpan":
                                st.error(f"🛑 {hasil['filename']} — GAGAL disimpan ke GCS: {hasil['error']}")
                                st.error("Proses batch dihentikan — GCS kemungkinan sedang bermasalah, cek dulu sebelum lanjut.")
                                break
                            else:
                                st.error(f"❌ {hasil['filename']} — gagal dibaca")
                            progress.progress((i + 1) / max(1, len(daftar_file)))

                except zipfile.BadZipFile:
                    st.error("File yang diupload bukan ZIP yang valid.")
                except Exception as e:
                    st.error(f"Gagal memproses ZIP: {e}")

        else:  # Dari Kaggle
            st.caption("Butuh KAGGLE_USERNAME & KAGGLE_KEY di Secrets — lihat catatan setup di bawah kode.")
            dataset_slug = st.text_input("Slug dataset Kaggle (mis. username/nama-dataset)")
            batas_gambar = st.number_input("Maksimal gambar yang diambil per proses", min_value=1, max_value=500, value=50)

            if st.button("Ambil & Daftarkan dari Kaggle", disabled=not dataset_slug):
                import os
                import tempfile

                try:
                    os.environ["KAGGLE_USERNAME"] = st.secrets["kaggle"]["username"]
                    os.environ["KAGGLE_KEY"] = st.secrets["kaggle"]["key"]
                    import kaggle

                    with tempfile.TemporaryDirectory() as tmpdir:
                        with st.spinner(f"Mengunduh dataset {dataset_slug} dari Kaggle..."):
                            kaggle.api.dataset_download_files(dataset_slug, path=tmpdir, unzip=True)

                        valid_ext = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
                        daftar_file = []
                        for root, _, files_ in os.walk(tmpdir):
                            for fn in files_:
                                if fn.lower().endswith(valid_ext):
                                    daftar_file.append(os.path.join(root, fn))
                        daftar_file = sorted(daftar_file)[: int(batas_gambar)]

                        st.write(f"Ditemukan {len(daftar_file)} gambar, mendaftarkan...")
                        progress = st.progress(0)
                        for i, path in enumerate(daftar_file):
                            with open(path, "rb") as fh:
                                file_bytes = fh.read()
                            hasil = register_image_bytes(
                                blockchain, file_bytes, os.path.basename(path), source_label="kaggle"
                            )
                            if hasil["status"] == "terdaftar":
                                st.success(f"✅ {hasil['filename']} — Block #{hasil['block_index']}")
                            elif hasil["status"] == "duplikat":
                                st.warning(f"⚠️ {hasil['filename']} — duplikat dari '{hasil['file_asli']}'")
                            elif hasil["status"] == "gagal_simpan":
                                st.error(f"🛑 {hasil['filename']} — GAGAL disimpan ke GCS: {hasil['error']}")
                                st.error("Proses batch dihentikan — GCS kemungkinan sedang bermasalah, cek dulu sebelum lanjut.")
                                break
                            else:
                                st.error(f"❌ {hasil['filename']} — gagal dibaca")
                            progress.progress((i + 1) / max(1, len(daftar_file)))

                except Exception as e:
                    st.error(f"Gagal mengambil dataset dari Kaggle: {e}")

# =========================================================================
# TAB VERIFIKASI — Ini yang buat semua orang, hati hati aja k
# =========================================================================
with tab_verifikasi:
    st.subheader("Verifikasi gambar suspek")
    files_suspek = st.file_uploader(
        "Upload gambar yang ingin dicek keasliannya",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        key="upload_suspek",
    )

    if st.button("Verifikasi", disabled=not files_suspek):
        hasil_list = []
        progress = st.progress(0, text="Memverifikasi...")
        for i, f in enumerate(files_suspek):
            hasil_list.append(verify_image_bytes(blockchain, f.read(), f.name))
            progress.progress((i + 1) / len(files_suspek))
        progress.empty()

        # =============== RINGKASAN JUMLAH ===============
        jumlah_real = sum(1 for h in hasil_list if h["status"] == "real")
        jumlah_fake = sum(1 for h in hasil_list if h["status"] == "fake")
        jumlah_tidak_dikenali = sum(1 for h in hasil_list if h["status"] in ("tidak_dikenali", "tidak_cocok"))
        jumlah_gagal = sum(1 for h in hasil_list if h["status"] == "gagal_baca")

        st.subheader("📊 Ringkasan")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total diproses", len(hasil_list))
        c2.metric("✅ Real", jumlah_real)
        c3.metric("❌ Fake", jumlah_fake)
        c4.metric("❓ Tidak dikenali", jumlah_tidak_dikenali)
        c5.metric("⚠️ Gagal dibaca", jumlah_gagal)
        st.divider()

        # =============== DETAIL PER GAMBAR ===============
        for hasil in hasil_list:
            st.markdown(f"### {hasil['filename']}")

            if hasil["status"] == "gagal_baca":
                st.error("Gagal membaca file gambar.")
            elif hasil["status"] == "tidak_cocok":
                st.error("Tidak cocok dengan gambar manapun di blockchain.")
            elif hasil["status"] == "tidak_dikenali":
                st.warning(f"Tidak dikenali — gap kecocokan {hasil['gap_percent']:.1f}% (di atas ambang batas).")
            else:
                col1, col2 = st.columns([1, 1])
                with col1:
                    st.image(hasil["overlay_image"], caption="Peta area terindikasi tamper (merah)")
                with col2:
                    if hasil["status"] == "fake":
                        st.error("❌ FAKE (Terindikasi dimanipulasi)")
                    else:
                        st.success("✅ REAL (Autentik)")
                    st.write(f"Cocok dengan gambar asli: **{hasil['matched_filename']}**")
                    st.write(f"Gap kecocokan hash: {hasil['gap_percent']:.1f}%")
                    if hasil["ada_teks"]:
                        st.write("🔤 Terindikasi ada overlay teks baru")
                    if hasil["resolusi_berubah"]:
                        st.write(
                            f"📐 Resolusi berubah: asli {hasil['resolusi_asli']} → suspek {hasil['resolusi_suspek']}"
                        )
                    if hasil["kompresi_bertambah"]:
                        st.write(f"🗜️ Kompresi bertambah (Δblockiness={hasil['delta_blockiness']:.2f})")
            st.divider()

# =========================================================================
# TAB KELOLA LEDGER (admin only): Buat ngecek blockchainnya biar gk error
# =========================================================================
if is_admin:
    with tab_kelola:
        st.subheader("Kelola data yang terdaftar di blockchain")
        tampilkan_terhapus = st.checkbox("Tampilkan juga yang sudah dihapus", value=False)

        entri = [b for b in blockchain.chain[1:] if tampilkan_terhapus or not b.metadata.get("deleted")]
        if not entri:
            st.info("Belum ada citra terdaftar.")

        for block in entri:
            is_deleted = bool(block.metadata.get("deleted"))
            col1, col2, col3 = st.columns([3, 2, 1])
            with col1:
                label = block.metadata.get("filename", "Unknown")
                st.markdown(f"~~{label}~~ *(dihapus)*" if is_deleted else label)
            with col2:
                st.caption(f"Block #{block.index} · sumber: {block.metadata.get('source', '-')}")
            with col3:
                if is_deleted:
                    if st.button("Pulihkan", key=f"restore_{block.index}"):
                        blockchain.restore_block(block.index)
                        st.rerun()
                else:
                    if st.button("Hapus", key=f"delete_{block.index}"):
                        blockchain.soft_delete_block(block.index, deleted_by=st.user.email)
                        st.rerun()
