package dev.lotus.lanmihome

import org.bouncycastle.crypto.engines.AESEngine
import org.bouncycastle.crypto.modes.CCMBlockCipher
import org.bouncycastle.crypto.params.AEADParameters
import org.bouncycastle.crypto.params.KeyParameter

const val SENSOR_PRODUCT_ID = 21941 // 0x55B5, xiaomi.sensor_ht.mini / MJWSD06MMC

data class MiBeaconSensorFrame(
    val productId: Int,
    val frameCounter: Int,
    val mac: String,
    val encrypted: Boolean,
    val authenticated: Boolean,
    val temperature: Double? = null,
    val humidity: Double? = null,
    val battery: Int? = null,
    val raw: String,
    val plain: String? = null,
)

object MiBeaconV5 {
    fun extractFe95(record: ByteArray): ByteArray? {
        var i = 0
        while (i < record.size) {
            val len = record[i].toInt() and 0xff
            if (len == 0) break
            val end = i + 1 + len
            if (end > record.size || i + 1 >= record.size) break
            val type = record[i + 1].toInt() and 0xff
            if (type == 0x16 && len >= 3 && i + 3 < end) {
                val lo = record[i + 2].toInt() and 0xff
                val hi = record[i + 3].toInt() and 0xff
                if (lo == 0x95 && hi == 0xfe) return record.copyOfRange(i + 4, end)
            }
            i = end
        }
        return null
    }

    fun decodeSensor(data: ByteArray, bindKey: ByteArray?): MiBeaconSensorFrame? {
        if (data.size < 11) return null
        val pid = u16le(data, 2)
        if (pid != SENSOR_PRODUCT_ID) return null

        val frameCounter = data[4].toInt() and 0xff
        val macBytes = data.copyOfRange(5, 11)
        val mac = macBytes.reversedArray().joinToString(":") { "%02X".format(it.toInt() and 0xff) }
        val encrypted = (data[0].toInt() and 0x08) != 0
        val raw = data.hex()

        val plain = if (encrypted) {
            if (bindKey == null || bindKey.size != 16 || data.size < 18) {
                return MiBeaconSensorFrame(pid, frameCounter, mac, true, false, raw = raw)
            }
            decrypt(data, bindKey) ?: return MiBeaconSensorFrame(pid, frameCounter, mac, true, false, raw = raw)
        } else {
            data.copyOfRange(11, data.size)
        }

        var temperature: Double? = null
        var humidity: Double? = null
        var battery: Int? = null
        var off = 0
        while (off + 3 <= plain.size) {
            val type = u16le(plain, off)
            val len = plain[off + 2].toInt() and 0xff
            val start = off + 3
            val end = start + len
            if (end > plain.size) break
            when (type) {
                0x1004 -> if (len >= 2) temperature = s16le(plain, start) / 10.0
                0x1006 -> if (len >= 2) humidity = u16le(plain, start) / 10.0
                0x100A -> if (len >= 1) battery = plain[start].toInt() and 0xff
                0x100D -> if (len >= 4) {
                    temperature = s16le(plain, start) / 10.0
                    humidity = u16le(plain, start + 2) / 10.0
                }
            }
            off = end
        }

        return MiBeaconSensorFrame(
            productId = pid,
            frameCounter = frameCounter,
            mac = mac,
            encrypted = encrypted,
            authenticated = true,
            temperature = temperature,
            humidity = humidity,
            battery = battery,
            raw = raw,
            plain = plain.hex(),
        )
    }

    private fun decrypt(data: ByteArray, key: ByteArray): ByteArray? = runCatching {
        val cipherEnd = data.size - 7
        if (cipherEnd < 11) return null
        val cipherText = data.copyOfRange(11, cipherEnd)
        val payloadCounter = data.copyOfRange(data.size - 7, data.size - 4)
        val tag = data.copyOfRange(data.size - 4, data.size)
        val nonce = ByteArray(12).also {
            System.arraycopy(data, 5, it, 0, 6)
            System.arraycopy(data, 2, it, 6, 3)
            System.arraycopy(payloadCounter, 0, it, 9, 3)
        }
        val ccm = CCMBlockCipher(AESEngine())
        ccm.init(false, AEADParameters(KeyParameter(key), 32, nonce, byteArrayOf(0x11)))
        val input = cipherText + tag
        val out = ByteArray(ccm.getOutputSize(input.size))
        var n = ccm.processBytes(input, 0, input.size, out, 0)
        n += ccm.doFinal(out, n)
        out.copyOf(n)
    }.getOrNull()

    private fun u16le(b: ByteArray, off: Int) =
        (b[off].toInt() and 0xff) or ((b[off + 1].toInt() and 0xff) shl 8)

    private fun s16le(b: ByteArray, off: Int): Int {
        val u = u16le(b, off)
        return if (u and 0x8000 != 0) u - 0x10000 else u
    }
}

fun ByteArray.hex(): String = joinToString("") { "%02x".format(it.toInt() and 0xff) }
