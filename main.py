import streamlit as st

from forensics_core import SecureBlockchain, register_image_bytes, verify_image_bytes

# GANTI sesuai bucket GCS yang sudah kamu buat
BUCKET_NAME = "nama-bucket-kamu"
LEDGER_BLOB_PATH = "ledger/opsi_ledger.json"

st.set_page_config(page_title="OPSI — Verifikasi Keaslian Citra", layout="wide")
st.title("🔗 OPSI — Sistem Verifikasi Keaslian Citra")


@st.cache_resource
def get_blockchain():
    # Kredensial GCP diambil dari Streamlit Secrets (Manage app > Settings > Secrets),
    # WAJIB karena Streamlit Cloud gak punya default credentials otomatis seperti Cloud Run.
    creds_info = dict(st.secrets["gcp_service_account"])
    return SecureBlockchain(bucket_name=BUCKET_NAME, blob_path=LEDGER_BLOB_PATH, credentials_info=creds_info)


blockchain = get_blockchain()
st.caption(f"Ledger saat ini: **{len(blockchain.chain) - 1}** citra asli terdaftar di GCS.")

tab_daftar, tab_verifikasi = st.tabs(["📝 Registrasi Gambar Asli", "🔍 Verifikasi Gambar Suspek"])

# =========================================================================
# TAB 1 — REGISTRASI (dulu Cell 5, sumber gdrive/kaggle → sekarang upload manual)
# =========================================================================
with tab_daftar:
    st.subheader("Daftarkan gambar asli ke blockchain")
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
        st.caption(f"Ledger sekarang: **{len(blockchain.chain) - 1}** citra asli terdaftar.")

# =========================================================================
# TAB 2 — VERIFIKASI (dulu Cell 6, sumber gdrive/kaggle → sekarang upload manual)
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
