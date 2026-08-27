import streamlit as st
import datetime
import time

from forensics_core import (
    SecureBlockchain,
    register_image_bytes,
    verify_image_bytes,
    extract_images_from_zip,
)
import zipfile

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
                waktu_mulai_batch = time.time()
                for f in files_asli:
                    hasil = register_image_bytes(blockchain, f.read(), f.name)
                    if hasil["status"] == "terdaftar":
                        st.success(f"✅ {hasil['filename']} — terdaftar (Block #{hasil['block_index']}, {hasil['durasi_proses']:.2f}s)")
                    elif hasil["status"] == "duplikat":
                        st.warning(f"⚠️ {hasil['filename']} — sudah terdaftar sebelumnya sebagai '{hasil['file_asli']}'")
                    elif hasil["status"] == "gagal_simpan":
                        st.error(f"🛑 {hasil['filename']} — GAGAL disimpan ke GCS (data BELUM terdaftar): {hasil['error']}")
                    else:
                        st.error(f"❌ {hasil['filename']} — gagal dibaca, cek format filenya")
                total_durasi_batch = time.time() - waktu_mulai_batch
                st.info(
                    f"⏱️ Selesai dalam {total_durasi_batch:.2f} detik untuk {len(files_asli)} gambar "
                    f"(rata-rata {total_durasi_batch / max(1, len(files_asli)):.2f} detik/gambar)"
                )

        elif sumber == "Upload ZIP (folder terkompresi)":
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
                try:
                    with st.spinner("Mengekstrak ZIP..."):
                        daftar_file = extract_images_from_zip(zip_file, filter_substring, batas_gambar_zip)

                    if not daftar_file:
                        st.warning("Gak ada gambar yang cocok ditemukan di dalam zip (cek lagi filter nama-nya).")

                    st.write(f"Ditemukan {len(daftar_file)} gambar yang cocok, mendaftarkan...")
                    progress = st.progress(0)
                    waktu_mulai_batch = time.time()
                    jumlah_diproses = 0
                    for i, (fname, fbytes) in enumerate(daftar_file):
                        hasil = register_image_bytes(blockchain, fbytes, fname, source_label="zip_upload")
                        jumlah_diproses += 1
                        if hasil["status"] == "terdaftar":
                            st.success(f"✅ {hasil['filename']} — Block #{hasil['block_index']} ({hasil['durasi_proses']:.2f}s)")
                        elif hasil["status"] == "duplikat":
                            st.warning(f"⚠️ {hasil['filename']} — duplikat dari '{hasil['file_asli']}'")
                        elif hasil["status"] == "gagal_simpan":
                            st.error(f"🛑 {hasil['filename']} — GAGAL disimpan ke GCS: {hasil['error']}")
                            st.error("Proses batch dihentikan — GCS kemungkinan sedang bermasalah, cek dulu sebelum lanjut.")
                            break
                        else:
                            st.error(f"❌ {hasil['filename']} — gagal dibaca")
                        progress.progress((i + 1) / max(1, len(daftar_file)))
                    total_durasi_batch = time.time() - waktu_mulai_batch
                    st.info(
                        f"⏱️ Selesai dalam {total_durasi_batch:.2f} detik untuk {jumlah_diproses} gambar "
                        f"(rata-rata {total_durasi_batch / max(1, jumlah_diproses):.2f} detik/gambar)"
                    )

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
                        waktu_mulai_batch = time.time()
                        jumlah_diproses = 0
                        for i, path in enumerate(daftar_file):
                            with open(path, "rb") as fh:
                                file_bytes = fh.read()
                            hasil = register_image_bytes(
                                blockchain, file_bytes, os.path.basename(path), source_label="kaggle"
                            )
                            jumlah_diproses += 1
                            if hasil["status"] == "terdaftar":
                                st.success(f"✅ {hasil['filename']} — Block #{hasil['block_index']} ({hasil['durasi_proses']:.2f}s)")
                            elif hasil["status"] == "duplikat":
                                st.warning(f"⚠️ {hasil['filename']} — duplikat dari '{hasil['file_asli']}'")
                            elif hasil["status"] == "gagal_simpan":
                                st.error(f"🛑 {hasil['filename']} — GAGAL disimpan ke GCS: {hasil['error']}")
                                st.error("Proses batch dihentikan — GCS kemungkinan sedang bermasalah, cek dulu sebelum lanjut.")
                                break
                            else:
                                st.error(f"❌ {hasil['filename']} — gagal dibaca")
                            progress.progress((i + 1) / max(1, len(daftar_file)))
                        total_durasi_batch = time.time() - waktu_mulai_batch
                        st.info(
                            f"⏱️ Selesai dalam {total_durasi_batch:.2f} detik untuk {jumlah_diproses} gambar "
                            f"(rata-rata {total_durasi_batch / max(1, jumlah_diproses):.2f} detik/gambar)"
                        )

                except Exception as e:
                    st.error(f"Gagal mengambil dataset dari Kaggle: {e}")

# =========================================================================
# TAB VERIFIKASI — Ini yang buat semua orang, hati hati aja k
# =========================================================================
with tab_verifikasi:
    st.subheader("Verifikasi gambar suspek")
    sumber_verif = st.radio(
        "Sumber gambar suspek",
        ["Upload manual", "Upload ZIP (folder terkompresi)"],
        horizontal=True,
        key="sumber_verif",
    )

    daftar_suspek = None  # list of (filename, bytes), diisi tergantung sumber yang dipilih

    if sumber_verif == "Upload manual":
        files_suspek = st.file_uploader(
            "Upload gambar yang ingin dicek keasliannya",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
            key="upload_suspek",
        )
        if st.button("Verifikasi", disabled=not files_suspek):
            daftar_suspek = [(f.name, f.read()) for f in files_suspek]

    else:  # Upload ZIP
        st.caption("Upload satu file .zip berisi banyak gambar suspek — folder/subfolder bertingkat otomatis dicari semua.")
        zip_file_verif = st.file_uploader("Upload file .zip", type=["zip"], key="upload_zip_verif")
        batas_verif_zip = st.number_input(
            "Maksimal gambar yang diproses per klik", min_value=1, max_value=2000, value=200, key="batas_zip_verif"
        )
        if st.button("Ekstrak & Verifikasi ZIP", disabled=not zip_file_verif):
            try:
                with st.spinner("Mengekstrak ZIP..."):
                    daftar_suspek = extract_images_from_zip(zip_file_verif, limit=batas_verif_zip)
                if not daftar_suspek:
                    st.warning("Gak ada gambar ditemukan di dalam zip.")
            except zipfile.BadZipFile:
                st.error("File yang diupload bukan ZIP yang valid.")
            except Exception as e:
                st.error(f"Gagal memproses ZIP: {e}")

    if daftar_suspek:
        hasil_list = []
        progress = st.progress(0, text="Memverifikasi...")
        waktu_mulai_batch = time.time()
        for i, (fname, fbytes) in enumerate(daftar_suspek):
            hasil_list.append(verify_image_bytes(blockchain, fbytes, fname))
            progress.progress((i + 1) / len(daftar_suspek))
        total_durasi_batch = time.time() - waktu_mulai_batch
        progress.empty()

        # =============== RINGKASAN JUMLAH & WAKTU ===============
        jumlah_real = sum(1 for h in hasil_list if h["status"] == "real")
        jumlah_fake = sum(1 for h in hasil_list if h["status"] == "fake")
        jumlah_tidak_dikenali = sum(1 for h in hasil_list if h["status"] in ("tidak_dikenali", "tidak_cocok"))
        jumlah_gagal = sum(1 for h in hasil_list if h["status"] == "gagal_baca")
        rata_rata_durasi = total_durasi_batch / max(1, len(hasil_list))

        st.subheader("📊 Ringkasan")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total diproses", len(hasil_list))
        c2.metric("✅ Real", jumlah_real)
        c3.metric("❌ Fake", jumlah_fake)
        c4.metric("❓ Tidak dikenali", jumlah_tidak_dikenali)
        c5.metric("⚠️ Gagal dibaca", jumlah_gagal)

        t1, t2 = st.columns(2)
        t1.metric("⏱️ Waktu total", f"{total_durasi_batch:.2f} detik")
        t2.metric("⏱️ Rata-rata / gambar", f"{rata_rata_durasi:.2f} detik")
        st.divider()

        # =============== DETAIL PER GAMBAR ===============
        for hasil in hasil_list:
            st.markdown(f"### {hasil['filename']}")
            durasi_gambar = hasil.get("durasi_proses")
            durasi_caption = f"⏱️ Diproses dalam {durasi_gambar:.2f} detik" if durasi_gambar is not None else ""

            if hasil["status"] == "gagal_baca":
                st.error("Gagal membaca file gambar.")
                if durasi_caption:
                    st.caption(durasi_caption)
            elif hasil["status"] == "tidak_cocok":
                st.error("Tidak cocok dengan gambar manapun di blockchain.")
                if durasi_caption:
                    st.caption(durasi_caption)
            elif hasil["status"] == "tidak_dikenali":
                st.warning(f"Tidak dikenali — gap kecocokan {hasil['gap_percent']:.1f}% (di atas ambang batas).")
                if durasi_caption:
                    st.caption(durasi_caption)
            else:
                col1, col2 = st.columns([1, 1])
                with col1:
                    st.image(hasil["overlay_image"], caption="Peta area terindikasi tamper (merah)")
                    if durasi_caption:
                        st.caption(durasi_caption)
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
        entri_aktif = [b for b in blockchain.chain[1:] if not b.metadata.get("deleted")]

        if not entri:
            st.info("Belum ada citra terdaftar.")

        # ================= HAPUS SEMUA SEKALIGUS =================
        if entri_aktif:
            with st.expander(f"🗑️ Hapus SEMUA entri aktif sekaligus ({len(entri_aktif)} entri)"):
                st.warning(
                    "Menandai SEMUA entri aktif sebagai terhapus dalam SATU kali simpan ke GCS "
                    "(cepat, gak peduli berapa banyak entrinya). Masih bisa dipulihkan satu-satu "
                    "lewat tombol 'Pulihkan' kalau keliru."
                )
                konfirmasi = st.text_input(
                    'Ketik "HAPUS SEMUA" (persis) untuk mengaktifkan tombolnya', key="konfirmasi_hapus_semua"
                )
                if st.button("Hapus SEMUA entri aktif", disabled=(konfirmasi != "HAPUS SEMUA")):
                    try:
                        with st.spinner(f"Menghapus {len(entri_aktif)} entri..."):
                            jumlah = blockchain.soft_delete_blocks(
                                [b.index for b in entri_aktif], deleted_by=st.user.email
                            )
                        st.success(f"✅ {jumlah} entri berhasil dihapus (1 kali simpan ke GCS).")
                        st.rerun()
                    except Exception as e:
                        st.error(f"🛑 Gagal menghapus: {e}")

        st.divider()

        # ================= HAPUS TERPILIH (checkbox) =================
        st.caption("Atau centang beberapa entri di bawah, lalu hapus sekaligus:")

        terpilih_untuk_hapus = []
        for block in entri:
            is_deleted = bool(block.metadata.get("deleted"))
            col0, col1, col2, col3 = st.columns([0.4, 3, 2, 1])
            with col0:
                if not is_deleted:
                    if st.checkbox("pilih", key=f"pilih_{block.index}", label_visibility="collapsed"):
                        terpilih_untuk_hapus.append(block.index)
            with col1:
                label = block.metadata.get("filename", "Unknown")
                st.markdown(f"~~{label}~~ *(dihapus)*" if is_deleted else label)
            with col2:
                waktu_str = datetime.datetime.fromtimestamp(block.timestamp).strftime("%d/%m/%Y, %H:%M:%S")
                durasi = block.metadata.get("processing_duration_seconds")
                durasi_str = f" · diproses dalam {durasi:.2f}s" if durasi is not None else ""
                st.caption(f"Block #{block.index} · sumber: {block.metadata.get('source', '-')}")
                st.caption(f"🗓️ {waktu_str}{durasi_str}")
            with col3:
                if is_deleted:
                    if st.button("Pulihkan", key=f"restore_{block.index}"):
                        blockchain.restore_block(block.index)
                        st.rerun()
                else:
                    if st.button("Hapus", key=f"delete_{block.index}"):
                        blockchain.soft_delete_block(block.index, deleted_by=st.user.email)
                        st.rerun()

        if terpilih_untuk_hapus:
            st.divider()
            if st.button(f"🗑️ Hapus {len(terpilih_untuk_hapus)} entri yang dicentang"):
                try:
                    with st.spinner(f"Menghapus {len(terpilih_untuk_hapus)} entri..."):
                        jumlah = blockchain.soft_delete_blocks(terpilih_untuk_hapus, deleted_by=st.user.email)
                    st.success(f"✅ {jumlah} entri berhasil dihapus (1 kali simpan ke GCS).")
                    st.rerun()
                except Exception as e:
                    st.error(f"🛑 Gagal menghapus: {e}")
