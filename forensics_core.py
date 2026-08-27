import time
import json
import hashlib

import cv2
import numpy as np
import pywt
import scipy.fftpack as fftpack
from google.cloud import storage
from google.oauth2 import service_account


# =========================================================================
# PRE PROCESS & HASHING
# =========================================================================

def preprocess_image(img):
    img_resized = cv2.resize(img, (512, 512))
    if len(img_resized.shape) == 3:
        return cv2.cvtColor(img_resized, cv2.COLOR_BGR2YCrCb)
    return img_resized


def extract_block_hash(block_ycbcr):
    y, cr, cb = cv2.split(block_ycbcr) if len(block_ycbcr.shape) == 3 else [block_ycbcr] * 3

    coeffs = pywt.dwt2(y, 'haar')
    LL, _ = coeffs
    dct_y = fftpack.dct(fftpack.dct(LL.T, norm='ortho').T, norm='ortho')

    # FIX #1: buang suku DC (index 0 hasil flatten) dari perhitungan mean —
    # DC (~rata-rata kecerahan blok) besarnya bisa puluhan-ratusan kali lipat
    # koefisien AC di sekitarnya, sehingga kalau ikut dihitung, mean-nya
    # "ketarik" jauh ke atas dan hampir semua bit di kuadran ini otomatis
    # kebaca '0' apa pun isi bloknya. Praktik standar pHash: threshold
    # dihitung dari AC saja, DC-nya sendiri tetap dibandingkan seperti biasa.
    dct_low_flat = dct_y[0:16, 0:16].flatten()
    mean_low = np.mean(dct_low_flat[1:])
    phash_struktur = "".join(['1' if val > mean_low else '0' for val in dct_low_flat])

    dct_mid = dct_y[16:32, 16:32]  # gak mengandung DC (mulai dari index 16), gak perlu diubah
    phash_struktur += "".join(['1' if val > np.mean(dct_mid) else '0' for val in dct_mid.flatten()])

    # FIX #2: tambah rata-rata Y (luma) per kuadran, bukan cuma Cr/Cb —
    # watermark teks putih/abu-abu/hitam mengubah luma drastis tapi nyaris
    # gak menggeser chroma (Cr/Cb netral untuk warna achromatic), jadi tanpa
    # Y di sini 'warna_berubah' secara desain buta terhadap watermark teks.
    h, w = cr.shape
    half_h, half_w = h // 2, w // 2
    color_stats = []
    for (r0, r1) in [(0, half_h), (half_h, h)]:
        for (c0, c1) in [(0, half_w), (half_w, w)]:
            color_stats.append(float(np.mean(y[r0:r1, c0:c1])))
            color_stats.append(float(np.mean(cr[r0:r1, c0:c1])))
            color_stats.append(float(np.mean(cb[r0:r1, c0:c1])))

    return phash_struktur, color_stats


def generate_grid_hashes(img_ycbcr, grid_size=4):
    h, w = img_ycbcr.shape[:2]
    step_h, step_w = h // grid_size, w // grid_size
    grid_hashes, grid_colors = [], []
    for i in range(grid_size):
        row_hashes, row_colors = [], []
        for j in range(grid_size):
            block = img_ycbcr[i * step_h:(i + 1) * step_h, j * step_w:(j + 1) * step_w]
            h_struct, c_stats = extract_block_hash(block)
            row_hashes.append(h_struct)
            row_colors.append(c_stats)
        grid_hashes.append(row_hashes)
        grid_colors.append(row_colors)
    return grid_hashes, grid_colors


def calculate_hamming_distance(h1, h2):
    if len(h1) != len(h2):
        return float('inf')
    return sum(c1 != c2 for c1, c2 in zip(h1, h2))


def build_text_mask(img_ycbcr, canny_low=50, canny_high=150, dilate_size=3):
    y = img_ycbcr[:, :, 0].astype(np.uint8)
    edges = cv2.Canny(y, canny_low, canny_high)
    kernel = np.ones((dilate_size, dilate_size), np.uint8)
    text_mask = cv2.dilate(edges, kernel, iterations=1)
    return (text_mask > 0).astype(np.float32)


def compute_hh_subband(img_ycbcr):
    y = img_ycbcr[:, :, 0].astype(np.float64)
    _, (_, _, HH) = pywt.dwt2(y, 'haar')
    return HH


def compute_masked_block_stats(HH_matrix, inverse_mask, grid_size=4, min_valid_fraction=0.1):
    h, w = HH_matrix.shape
    step_h, step_w = h // grid_size, w // grid_size
    total_pixel_blok = step_h * step_w
    min_valid_pixels = min_valid_fraction * total_pixel_blok

    mask_resized = cv2.resize(inverse_mask, (w, h), interpolation=cv2.INTER_AREA)

    valid_pixels_map, variance_map = [], []
    for i in range(grid_size):
        row_vp, row_var = [], []
        for j in range(grid_size):
            patch = HH_matrix[i * step_h:(i + 1) * step_h, j * step_w:(j + 1) * step_w]
            mask_patch = mask_resized[i * step_h:(i + 1) * step_h, j * step_w:(j + 1) * step_w]
            valid_pixels = float(np.sum(mask_patch))
            row_vp.append(valid_pixels)
            if valid_pixels > min_valid_pixels:
                row_var.append(float(np.sum(np.abs(patch) * mask_patch) / valid_pixels))
            else:
                row_var.append(-1.0)
        valid_pixels_map.append(row_vp)
        variance_map.append(row_var)
    return valid_pixels_map, variance_map


def measure_jpeg_blockiness(img, block_size=8):
    """REKONSTRUKSI — lihat catatan di docstring modul ini."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float64)
    diff_h = np.abs(np.diff(gray, axis=1))

    boundary_cols = np.arange(block_size - 1, diff_h.shape[1], block_size)
    all_cols = np.arange(diff_h.shape[1])
    non_boundary_cols = np.setdiff1d(all_cols, boundary_cols)

    boundary_energy = np.mean(diff_h[:, boundary_cols]) if len(boundary_cols) > 0 else 0.0
    non_boundary_energy = np.mean(diff_h[:, non_boundary_cols]) if len(non_boundary_cols) > 0 else 1e-6

    return float(boundary_energy / (non_boundary_energy + 1e-6))


# =========================================================================
# BK-TREE & BLOCKCHAIN
# =========================================================================

class BKTreeNode:
    def __init__(self, item):
        self.item = item
        self.children = {}


class BKTree:
    def __init__(self, distance_func=calculate_hamming_distance):
        self.root = None
        self.distance_func = distance_func

    def _get_representative_hash(self, item):
        grid_hashes = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else item
        if isinstance(grid_hashes, list):
            if len(grid_hashes) > 0 and isinstance(grid_hashes[0], list):
                return "".join([str(h) for row in grid_hashes for h in row])
            return "".join([str(h) for h in grid_hashes])
        return str(grid_hashes)

    def insert(self, item):
        if self.root is None:
            self.root = BKTreeNode(item)
            return
        node = self.root
        item_hash = self._get_representative_hash(item)
        while True:
            curr_hash = self._get_representative_hash(node.item)
            dist = self.distance_func(item_hash, curr_hash)
            if dist in node.children:
                node = node.children[dist]
            else:
                node.children[dist] = BKTreeNode(item)
                break


class Block:
    def __init__(self, index, timestamp, grid_hashes, metadata, previous_hash,
                 grid_colors=None, text_valid_pixels_map=None, text_variance_map=None):
        self.index = index
        self.timestamp = timestamp
        self.grid_hashes = grid_hashes
        self.grid_colors = grid_colors
        self.text_valid_pixels_map = text_valid_pixels_map
        self.text_variance_map = text_variance_map
        self.metadata = metadata
        self.meta = metadata
        self.previous_hash = previous_hash
        self.tx_id = self.calculate_txid()
        self.hash = self.tx_id

    def calculate_txid(self):
        block_string = (f"{self.index}{self.timestamp}{json.dumps(self.grid_hashes)}"
                         f"{json.dumps(self.grid_colors)}{json.dumps(self.text_valid_pixels_map)}"
                         f"{json.dumps(self.text_variance_map)}{json.dumps(self.metadata)}{self.previous_hash}")
        return hashlib.sha256(block_string.encode()).hexdigest()

    def to_dict(self):
        return {
            "index": self.index, "timestamp": self.timestamp,
            "grid_hashes": self.grid_hashes, "grid_colors": self.grid_colors,
            "text_valid_pixels_map": self.text_valid_pixels_map,
            "text_variance_map": self.text_variance_map,
            "metadata": self.metadata,
            "previous_hash": self.previous_hash, "tx_id": self.tx_id, "hash": self.tx_id,
        }


class SecureBlockchain:
    """
    Sama seperti versi Colab-mu, tapi load_db()/save_db() sekarang baca-tulis
    ke sebuah blob di Google Cloud Storage, bukan file lokal opsi_ledger.json —
    supaya ledger gak hilang tiap kali instance Cloud Run/Streamlit restart.
    """

    def __init__(self, bucket_name, blob_path="ledger/opsi_ledger.json", credentials_info=None):
        """
        credentials_info: dict isi service account JSON (mis. dari st.secrets),
        WAJIB diisi kalau dijalankan di luar GCP (Streamlit Cloud, HF Spaces, dll)
        karena di sana gak ada default credentials otomatis seperti di Cloud Run.
        """
        self.bucket_name = bucket_name
        self.blob_path = blob_path

        if credentials_info is not None:
            credentials = service_account.Credentials.from_service_account_info(credentials_info)
            self._client = storage.Client(credentials=credentials, project=credentials_info.get("project_id"))
        else:
            # Cuma jalan kalau ada default credentials otomatis (mis. di Cloud Run/Compute Engine)
            self._client = storage.Client()

        self._bucket = self._client.bucket(bucket_name)
        self.chain = []
        self.bk_tree = BKTree()
        self.load_db()

    # JANGAN dibungkus try/except-diam-diam lagi di sini — kalau GCS gagal,
    # error-nya HARUS nyampe ke caller (yang lalu ditangkap di register_image
    # untuk rollback + dikasih tau ke UI). Ini pernah bikin bug "data ilang
    # diam-diam" waktu errornya cuma di-print ke log doang.
    def save_db(self):
        data = json.dumps([b.to_dict() for b in self.chain], indent=2)
        blob = self._bucket.blob(self.blob_path)
        blob.upload_from_string(data, content_type="application/json")

    # Sama seperti save_db() — HANYA fallback ke genesis kalau blob-nya memang
    # belum pernah ada (blob.exists() == False). Kalau ada ERROR pas cek/baca
    # (mis. 403 billing/izin), JANGAN ditangkap diam-diam terus bikin genesis
    # baru — itu bisa menimpa ledger asli yang sebenarnya masih ada di GCS.
    def load_db(self):
        blob = self._bucket.blob(self.blob_path)
        if blob.exists():
            data = json.loads(blob.download_as_text())
            self.chain = []
            self.bk_tree = BKTree()
            for item in data:
                b = Block(
                    item["index"], item["timestamp"], item["grid_hashes"],
                    item["metadata"], item["previous_hash"],
                    grid_colors=item.get("grid_colors"),
                    text_valid_pixels_map=item.get("text_valid_pixels_map"),
                    text_variance_map=item.get("text_variance_map"),
                )
                b.tx_id = item.get("tx_id", item.get("hash"))
                b.hash = b.tx_id
                self.chain.append(b)
                if b.index > 0:
                    self.bk_tree.insert((b.tx_id, b.grid_hashes, b.metadata, b))
        else:
            self.create_genesis_block()

    def create_genesis_block(self):
        self.chain = []
        self.bk_tree = BKTree()
        genesis = Block(0, time.time(), [], {"filename": "Genesis Block OPSI"}, "0")
        self.chain.append(genesis)
        self.save_db()

    def get_latest_block(self):
        return self.chain[-1]

    def check_duplicate(self, grid_hashes, threshold=0):
        new_flat_hash = ("".join([str(h) for row in grid_hashes for h in row])
                          if isinstance(grid_hashes[0], list) else "".join(grid_hashes))
        for block in self.chain[1:]:
            if block.metadata.get("deleted"):
                continue
            if not block.grid_hashes:
                continue
            block_flat_hash = ("".join([str(h) for row in block.grid_hashes for h in row])
                                if isinstance(block.grid_hashes[0], list) else "".join(block.grid_hashes))
            dist = calculate_hamming_distance(new_flat_hash, block_flat_hash)
            if dist <= threshold:
                return True, block
        return False, None

    def register_image(self, grid_hashes, metadata, grid_colors=None,
                        text_valid_pixels_map=None, text_variance_map=None):
        is_dup, existing_block = self.check_duplicate(grid_hashes, threshold=0)
        if is_dup:
            return None, existing_block

        latest = self.get_latest_block()
        new_block = Block(
            index=latest.index + 1, timestamp=time.time(),
            grid_hashes=grid_hashes, metadata=metadata, previous_hash=latest.tx_id,
            grid_colors=grid_colors,
            text_valid_pixels_map=text_valid_pixels_map, text_variance_map=text_variance_map,
        )
        self.chain.append(new_block)
        self.bk_tree.insert((new_block.tx_id, new_block.grid_hashes, new_block.metadata, new_block))
        try:
            self.save_db()
        except Exception as e:
            # ROLLBACK — batalkan penambahan di memori kalau GCS gagal, biar
            # state gak "pura-pura sukses" padahal belum benar-benar tersimpan.
            self.chain.pop()
            raise RuntimeError(f"Gagal menyimpan ke Google Cloud Storage: {e}") from e
        return new_block, None

    def soft_delete_block(self, index, deleted_by=None):

        for block in self.chain:
            if block.index == index:
                block.metadata["deleted"] = True
                block.metadata["deleted_at"] = time.time()
                if deleted_by:
                    block.metadata["deleted_by"] = deleted_by
                self.save_db()
                return True
        return False

    def restore_block(self, index):

      #Batalin soft delete
        for block in self.chain:
            if block.index == index:
                block.metadata.pop("deleted", None)
                block.metadata.pop("deleted_at", None)
                block.metadata.pop("deleted_by", None)
                self.save_db()
                return True
        return False

#---------------
#Alur registrasi
#---------------

THRESHOLDS = dict(
    TOLERANSI_BRIGHT=15.0,
    TOLERANSI_MID=23.0,
    TOLERANSI_DARK=27.0,
    TOLERANSI_BLOCKINESS=1.5,
    TOLERANSI_WARNA_ABS=6.0,
    MAX_MATCH_PERCENT=35.0,
    CANNY_LOW=50,
    CANNY_HIGH=150,
    DILATE_SIZE_TEXT=3,
    MIN_VALID_FRACTION_TEXT=0.1,
    TOLERANSI_TEKS_BARU=0.03,  # diturunkan dari 0.15 — kalibrasi lebih lanjut pakai debug_metrik_blok()
    TOLERANSI_VARIANCE_HH=50.0,
)


def register_image_bytes(blockchain, file_bytes, filename, source_label="upload"):
    """Daftarkan satu gambar (bytes) ke blockchain. Return dict hasil."""
    t0 = time.perf_counter()
    arr = np.frombuffer(file_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return {"status": "gagal_baca", "filename": filename, "durasi_proses": time.perf_counter() - t0}

    img_ycbcr = preprocess_image(img)
    grid_hashes, grid_colors = generate_grid_hashes(img_ycbcr, grid_size=4)

    text_mask = build_text_mask(
        img_ycbcr, THRESHOLDS["CANNY_LOW"], THRESHOLDS["CANNY_HIGH"], THRESHOLDS["DILATE_SIZE_TEXT"]
    )
    inverse_mask = 1.0 - text_mask
    HH_matrix = compute_hh_subband(img_ycbcr)
    text_valid_pixels_map, text_variance_map = compute_masked_block_stats(
        HH_matrix, inverse_mask, grid_size=4, min_valid_fraction=THRESHOLDS["MIN_VALID_FRACTION_TEXT"]
    )

    h_asli, w_asli = img.shape[:2]
    blockiness_asli = measure_jpeg_blockiness(img, block_size=8)

    # Durasi ANALISIS gambar (belum termasuk waktu upload ke GCS) — ini yang
    # disimpan PERMANEN ke metadata block, supaya kelihatan di tab Kelola Ledger.
    durasi_analisis = time.perf_counter() - t0

    meta = {
        "filename": filename,
        "source": source_label,
        "status": "Registered Asli",
        "width": w_asli,
        "height": h_asli,
        "blockiness_score": blockiness_asli,
        "processing_duration_seconds": round(durasi_analisis, 3),
    }

    new_block, existing_block = None, None
    try:
        new_block, existing_block = blockchain.register_image(
            grid_hashes, meta, grid_colors=grid_colors,
            text_valid_pixels_map=text_valid_pixels_map, text_variance_map=text_variance_map,
        )
    except RuntimeError as e:
        return {"status": "gagal_simpan", "filename": filename, "error": str(e),
                "durasi_proses": time.perf_counter() - t0}

    # Durasi TOTAL (termasuk waktu simpan ke GCS) — cuma buat ditampilkan
    # sekilas di UI batch, TIDAK disimpan ke ledger (beda dari yang di atas).
    durasi_total = time.perf_counter() - t0

    if new_block is not None:
        return {"status": "terdaftar", "filename": filename,
                "block_index": new_block.index, "tx_id": new_block.tx_id,
                "durasi_proses": durasi_total}
    else:
        orig_file = existing_block.metadata.get("filename", "Unknown")
        return {"status": "duplikat", "filename": filename,
                "block_index": existing_block.index, "file_asli": orig_file,
                "durasi_proses": durasi_total}


def verify_image_bytes(blockchain, file_bytes, filename):
    """Verifikasi satu gambar suspek (bytes) terhadap blockchain. Return dict hasil + overlay RGB array."""
    t0 = time.perf_counter()
    T = THRESHOLDS
    arr = np.frombuffer(file_bytes, np.uint8)
    img_suspect = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img_suspect is None:
        return {"status": "gagal_baca", "filename": filename, "durasi_proses": time.perf_counter() - t0}

    blockiness_suspect = measure_jpeg_blockiness(img_suspect, block_size=8)
    h_orig, w_orig = img_suspect.shape[:2]

    if max(h_orig, w_orig) > 1024:
        scale = 1024.0 / max(h_orig, w_orig)
        img_suspect_fast = cv2.resize(img_suspect, (int(w_orig * scale), int(h_orig * scale)),
                                       interpolation=cv2.INTER_AREA)
    else:
        img_suspect_fast = img_suspect

    img_suspect_ycbcr = preprocess_image(img_suspect_fast)
    hash_suspect, color_suspect = generate_grid_hashes(img_suspect_ycbcr, grid_size=4)

    text_mask_suspect = build_text_mask(img_suspect_ycbcr, T["CANNY_LOW"], T["CANNY_HIGH"], T["DILATE_SIZE_TEXT"])
    inverse_mask_suspect = 1.0 - text_mask_suspect
    HH_suspect = compute_hh_subband(img_suspect_ycbcr)
    valid_pixels_suspect, variance_suspect = compute_masked_block_stats(
        HH_suspect, inverse_mask_suspect, grid_size=4, min_valid_fraction=T["MIN_VALID_FRACTION_TEXT"]
    )
    total_pixel_blok_hh = (HH_suspect.shape[0] // 4) * (HH_suspect.shape[1] // 4)

    best_match_block = None
    min_total_hd = float('inf')
    TOTAL_BITS_STRUKTUR = 16 * 512
    for block in blockchain.chain[1:]:
        if block.metadata.get("deleted"):
            continue
        block_grid_hashes = block.grid_hashes
        if not block_grid_hashes:
            continue
        total_hd = 0
        for i in range(4):
            for j in range(4):
                total_hd += calculate_hamming_distance(block_grid_hashes[i][j], hash_suspect[i][j])
        if total_hd < min_total_hd:
            min_total_hd = total_hd
            best_match_block = block

    if best_match_block is None:
        return {"status": "tidak_cocok", "filename": filename, "durasi_proses": time.perf_counter() - t0}

    persen_gap_match = (min_total_hd / TOTAL_BITS_STRUKTUR) * 100
    if persen_gap_match > T["MAX_MATCH_PERCENT"]:
        return {"status": "tidak_dikenali", "filename": filename, "gap_percent": persen_gap_match,
                "durasi_proses": time.perf_counter() - t0}

    hash_asli = best_match_block.grid_hashes
    color_asli = best_match_block.grid_colors
    valid_pixels_asli = best_match_block.text_valid_pixels_map
    variance_asli = best_match_block.text_variance_map
    meta_data = best_match_block.metadata
    w_asli = meta_data.get('width')
    h_asli = meta_data.get('height')
    blockiness_asli = meta_data.get('blockiness_score')

    resolusi_berubah = (w_asli is not None and (w_orig != w_asli or h_orig != h_asli))

    kompresi_bertambah = False
    delta_blockiness = 0.0
    if blockiness_asli is not None:
        delta_blockiness = blockiness_suspect - blockiness_asli
        kompresi_bertambah = delta_blockiness > T["TOLERANSI_BLOCKINESS"]

    grid_size = 4
    y_channel = img_suspect_ycbcr[:, :, 0]
    step_h, step_w = img_suspect_ycbcr.shape[0] // grid_size, img_suspect_ycbcr.shape[1] // grid_size

    tamper_map = np.zeros((grid_size, grid_size))
    text_flag_map = np.zeros((grid_size, grid_size))
    is_tampered = False

    for i in range(grid_size):
        for j in range(grid_size):
            patch_y = y_channel[i * step_h:(i + 1) * step_h, j * step_w:(j + 1) * step_w]
            mean_brightness = np.mean(patch_y)
            thresh_str = (T["TOLERANSI_DARK"] if mean_brightness < 45.0
                          else (T["TOLERANSI_MID"] if mean_brightness < 80.0 else T["TOLERANSI_BRIGHT"]))

            hd_struktur = calculate_hamming_distance(hash_asli[i][j], hash_suspect[i][j])
            struktur_berubah = (hd_struktur / 512.0) * 100 > thresh_str

            warna_berubah = False
            if color_asli is not None:
                c_asli = color_asli[i][j]
                c_suspect = color_suspect[i][j]
                delta_warna = max(abs(a - b) for a, b in zip(c_asli, c_suspect))
                warna_berubah = delta_warna > T["TOLERANSI_WARNA_ABS"]

            if struktur_berubah or warna_berubah:
                tamper_map[i, j] = 1
                is_tampered = True

            teks_berubah_blok = False
            if valid_pixels_asli is not None:
                vp_asli = valid_pixels_asli[i][j]
                vp_suspect = valid_pixels_suspect[i][j]
                penyusutan_area_bersih = (vp_asli - vp_suspect) / total_pixel_blok_hh
                if penyusutan_area_bersih > T["TOLERANSI_TEKS_BARU"]:
                    teks_berubah_blok = True

                if variance_asli is not None:
                    var_asli = variance_asli[i][j]
                    var_suspect = variance_suspect[i][j]
                    if var_asli >= 0 and var_suspect >= 0:
                        if abs(var_asli - var_suspect) > T["TOLERANSI_VARIANCE_HH"]:
                            teks_berubah_blok = True

            if teks_berubah_blok:
                tamper_map[i, j] = 1
                text_flag_map[i, j] = 1
                is_tampered = True

    if resolusi_berubah or kompresi_bertambah:
        is_tampered = True

    img_rgb = cv2.cvtColor(img_suspect_ycbcr, cv2.COLOR_YCrCb2RGB)
    overlay = img_rgb.copy()
    for i in range(grid_size):
        for j in range(grid_size):
            if tamper_map[i, j] == 1:
                cv2.rectangle(overlay, (j * step_w, i * step_h), ((j + 1) * step_w, (i + 1) * step_h),
                              (255, 0, 0), -1)
            cv2.rectangle(img_rgb, (j * step_w, i * step_h), ((j + 1) * step_w, (i + 1) * step_h),
                          (255, 255, 255), 1)
    blended = cv2.addWeighted(overlay, 0.45, img_rgb, 0.55, 0)

    return {
        "status": "fake" if is_tampered else "real",
        "filename": filename,
        "matched_filename": meta_data.get("filename", "Unknown"),
        "gap_percent": persen_gap_match,
        "ada_teks": bool(np.any(text_flag_map == 1)),
        "resolusi_berubah": resolusi_berubah,
        "resolusi_asli": (w_asli, h_asli),
        "resolusi_suspek": (w_orig, h_orig),
        "kompresi_bertambah": kompresi_bertambah,
        "delta_blockiness": delta_blockiness,
        "overlay_image": blended,
        "durasi_proses": time.perf_counter() - t0,
    }


# =========================================================================
# HELPER ZIP — dipakai bersama oleh tab Registrasi & tab Verifikasi di
# main.py. JANGAN dihapus — main.py meng-import fungsi ini secara langsung.
# =========================================================================

VALID_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def extract_images_from_zip(zip_fileobj, filter_substring="", limit=None):
    """
    Ekstrak satu file .zip (bisa file-like object dari st.file_uploader) ke folder
    sementara, cari SEMUA gambar di dalamnya secara rekursif (sedalam apa pun
    struktur folder/subfolder-nya), opsional filter berdasarkan potongan nama file.

    Return: list of (filename, bytes) — sudah diurutkan berdasarkan nama.
    """
    import os
    import zipfile
    import tempfile

    hasil = []
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_fileobj) as zf:
            zf.extractall(tmpdir)

        daftar_path = []
        for root, _, files_ in os.walk(tmpdir):
            for fn in files_:
                if not fn.lower().endswith(VALID_IMAGE_EXTENSIONS):
                    continue
                if filter_substring and filter_substring.lower() not in fn.lower():
                    continue
                daftar_path.append(os.path.join(root, fn))
        daftar_path = sorted(daftar_path)
        if limit is not None:
            daftar_path = daftar_path[: int(limit)]

        for path in daftar_path:
            with open(path, "rb") as fh:
                hasil.append((os.path.basename(path), fh.read()))

    return hasil


# =========================================================================
# UTILITAS KALIBRASI (opsional) — TIDAK dipanggil oleh alur registrasi/
# verifikasi utama. Pakai manual dari notebook/shell kalau TOLERANSI_TEKS_BARU
# (atau ambang lain) perlu disetel ulang untuk kasus tamper spesifik.
# =========================================================================

def debug_metrik_blok(path_asli, path_suspect, i=3, j=3):
    """
    Cetak metrik mentah 1 blok grid (default kanan-bawah, i=3,j=3) dari
    sepasang file gambar lokal — buat nyari ambang yang pas sebelum ubah
    THRESHOLDS asal nebak. Bandingkan hasilnya di:
    (a) gambar asli vs versi yang ditamper (mis. ditambah watermark), dan
    (b) gambar asli vs versi yang cuma di-resave/dikompres ulang TANPA tamper
        — supaya tahu ambang aman yang gak salah tandai kompresi normal.
    """
    def proses(path):
        with open(path, "rb") as f:
            arr = np.frombuffer(f.read(), np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        img_ycbcr = preprocess_image(img)
        mask = build_text_mask(img_ycbcr, 50, 150, 3)
        HH = compute_hh_subband(img_ycbcr)
        return compute_masked_block_stats(HH, 1.0 - mask, grid_size=4, min_valid_fraction=0.1)

    vp_a, var_a = proses(path_asli)
    vp_s, var_s = proses(path_suspect)
    total_blok = (256 // 4) ** 2  # HH 256x256 (dari preprocess 512x512), dibagi grid 4x4
    penyusutan = (vp_a[i][j] - vp_s[i][j]) / total_blok
    print(f"valid_pixels  asli={vp_a[i][j]:.0f}  suspect={vp_s[i][j]:.0f}  -> penyusutan={penyusutan:.4f}")
    print(f"variance      asli={var_a[i][j]:.2f}  suspect={var_s[i][j]:.2f}  -> delta={abs(var_a[i][j]-var_s[i][j]):.2f}")
