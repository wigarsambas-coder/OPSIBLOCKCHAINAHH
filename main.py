import streamlit as st

st.set_page_config(page_title="OPSI — Verifikasi Keaslian Citra", layout="wide")

# =========================================================================
# GERBANG AKSES — seluruh aplikasi ini privat. Hanya email yang terdaftar
# di admin_emails (Secrets) yang boleh login DAN masuk ke halaman manapun.
# Kalau admin_emails belum diisi di Secrets, TIDAK ADA yang bisa masuk
# (fail-closed secara sengaja, bukan bug).
# =========================================================================
ADMIN_EMAILS = list(st.secrets.get("admin_emails", []))

if not st.user.is_logged_in:
    st.header("🔒 OPSI — Sistem Verifikasi Keaslian Citra")
    st.subheader("Aplikasi ini privat. Silakan login dengan akun Google yang terdaftar.")
    st.button("Login dengan Google", on_click=st.login)
    st.stop()

if st.user.email not in ADMIN_EMAILS:
    st.header("🔒 Akses ditolak")
    st.write(f"Akun **{st.user.email}** belum terdaftar sebagai admin aplikasi ini.")
    st.caption("Hubungi pengelola aplikasi kalau menurutmu ini keliru.")
    st.button("Log out", on_click=st.logout)
    st.stop()

# --- Dari titik ini ke bawah, dipastikan yang mengakses adalah admin yang sudah login ---
from forensics_core import SecureBlockchain, register_image_bytes, verify_image_bytes

BUCKET_NAME = "nama-bucket-kamu"  # GANTI sesuai bucket GCS-mu
LEDGER_BLOB_PATH = "ledger/opsi_ledger.json"


@st.cache_resource
def get_blockchain():
    creds_info = dict(st.secrets["gcp_service_account"])
    return SecureBlockchain(bucket_name=BUCKET_NAME, blob_path=LEDGER_BLOB_PATH, credentials_info=creds_info)


blockchain = get_blockchain()

with st.sidebar:
    st.write(f"👤 **{st.user.name}**")
    st.caption(st.user.email)
    st.button("Log out", on_click=st.logout)

st.title("🔗 OPSI — Sistem Verifikasi Keaslian Citra")
jumlah_aktif = sum(1 for b in blockchain.chain[1:] if not b.metadata.get("deleted"))
st.caption(f"Ledger saat ini: **{jumlah_aktif}** citra asli terdaftar (aktif) di GCS.")

tab_daftar, tab_verifikasi, tab_kelola = st.tabs(
    ["📝 Registrasi Gambar Asli", "🔍 Verifikasi Gambar Suspek", "🛡️ Kelola Ledger"]
)

# =========================================================================
# TAB 1 — REGISTRASI: upload manual ATAU tarik dari Kaggle
# =========================================================================
with tab_daftar:
    st.subheader("Daftarkan gambar asli ke blockchain")
    sumber = st.radio("Sumber gambar", ["Upload manual", "Dari Kaggle"], horizontal=True)

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
                else:
                    st.error(f"❌ {hasil['filename']} — gagal dibaca, cek format filenya")

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
                        else:
                            st.error(f"❌ {hasil['filename']} — gagal dibaca")
                        progress.progress((i + 1) / max(1, len(daftar_file)))

            except Exception as e:
                st.error(f"Gagal mengambil dataset dari Kaggle: {e}")

# =========================================================================
# TAB 2 — VERIFIKASI (tidak berubah dari versi sebelumnya)
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
        for f in files_suspek:
            hasil = verify_image_bytes(blockchain, f.read(), f.name)
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
# TAB 3 — KELOLA LEDGER (khusus admin): lihat semua entri & hapus (soft delete)
# =========================================================================
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
