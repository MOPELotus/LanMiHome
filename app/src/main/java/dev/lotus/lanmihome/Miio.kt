package dev.lotus.lanmihome

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Collections
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import javax.crypto.Cipher
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.SecretKeySpec

private data class MiioHello(val deviceId: Long, val stamp: Long)

internal class MiioClient(private val localAddress: Inet4Address?) {
    private val ids = AtomicInteger(0)

    fun discover(broadcast: Inet4Address?, timeoutMs: Int = 900): Set<Inet4Address> {
        if (broadcast == null) return emptySet()
        val found = linkedSetOf<Inet4Address>()
        socket(timeoutMs).use { socket ->
            socket.broadcast = true
            repeat(2) {
                socket.send(DatagramPacket(HELLO, HELLO.size, broadcast, 54321))
            }
            val deadline = SystemClock.elapsedRealtime() + timeoutMs
            while (true) {
                val remaining = (deadline - SystemClock.elapsedRealtime()).toInt()
                if (remaining <= 0) break
                socket.soTimeout = remaining
                val buf = ByteArray(1024)
                val packet = DatagramPacket(buf, buf.size)
                try {
                    socket.receive(packet)
                } catch (_: java.net.SocketTimeoutException) {
                    break
                }
                if (packet.length >= 32 && buf[0] == 0x21.toByte() && buf[1] == 0x31.toByte()) {
                    (packet.address as? Inet4Address)?.let { found += it }
                }
            }
        }
        return found
    }

    fun getProperties(host: Inet4Address, tokenHex: String, mapping: Map<String, Pair<Int, Int>>): Map<String, Any?> {
        val params = JSONArray()
        mapping.forEach { (did, ids) ->
            params.put(JSONObject().put("did", did).put("siid", ids.first).put("piid", ids.second))
        }
        val response = request(host, tokenHex, "get_properties", params)
        val result = response.optJSONArray("result") ?: throw java.io.IOException("MIoT response has no result")
        val out = mutableMapOf<String, Any?>()
        for (i in 0 until result.length()) {
            val item = result.optJSONObject(i) ?: continue
            if (item.optInt("code", 0) == 0 && item.has("value")) {
                out[item.optString("did")] = if (item.isNull("value")) null else item.get("value")
            }
        }
        return out
    }

    fun setProperty(host: Inet4Address, tokenHex: String, siid: Int, piid: Int, value: Any) {
        val params = JSONArray().put(
            JSONObject().put("did", "set-$siid-$piid").put("siid", siid).put("piid", piid).put("value", value)
        )
        ensureOk(request(host, tokenHex, "set_properties", params))
    }

    fun action(host: Inet4Address, tokenHex: String, siid: Int, aiid: Int, input: JSONArray = JSONArray()) {
        val params = JSONObject()
            .put("did", "call-$siid-$aiid")
            .put("siid", siid)
            .put("aiid", aiid)
            .put("in", input)
        ensureOk(request(host, tokenHex, "action", params))
    }

    private fun ensureOk(response: JSONObject) {
        response.optJSONObject("error")?.let { throw java.io.IOException("MIoT error ${it.optInt("code")}: ${it.optString("message")}") }
        val result = response.optJSONArray("result")
        if (result != null) {
            for (i in 0 until result.length()) {
                val item = result.optJSONObject(i) ?: continue
                val code = item.optInt("code", 0)
                if (code != 0) throw java.io.IOException("MIoT code $code")
            }
        } else {
            val obj = response.optJSONObject("result")
            val code = obj?.optInt("code", 0) ?: 0
            if (code != 0) throw java.io.IOException("MIoT code $code")
        }
    }

    private fun request(host: Inet4Address, tokenHex: String, method: String, params: Any): JSONObject {
        val token = tokenBytes(tokenHex)
        var last: Exception? = null
        repeat(2) {
            try {
                val hello = hello(host)
                val id = ids.updateAndGet { current -> if (current >= 9998) 1 else current + 1 }
                val request = JSONObject().put("id", id).put("method", method).put("params", params)
                val plain = (request.toString() + "\u0000").toByteArray(Charsets.UTF_8)
                val key = md5(token)
                val iv = md5(key + token)
                val cipher = Cipher.getInstance("AES/CBC/PKCS5Padding")
                cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), IvParameterSpec(iv))
                val encrypted = cipher.doFinal(plain)

                val header = ByteArray(16)
                ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN).apply {
                    putShort(0x2131.toShort())
                    putShort((32 + encrypted.size).toShort())
                    putInt(0)
                    putInt(hello.deviceId.toInt())
                    putInt((hello.stamp + 1).toInt())
                }
                val checksum = md5(header + token + encrypted)
                val packet = header + checksum + encrypted

                val responseBytes = socket(2500).use { socket ->
                    socket.send(DatagramPacket(packet, packet.size, host, 54321))
                    val buf = ByteArray(8192)
                    val response = DatagramPacket(buf, buf.size)
                    socket.receive(response)
                    buf.copyOf(response.length)
                }
                if (responseBytes.size < 32 || responseBytes[0] != 0x21.toByte() || responseBytes[1] != 0x31.toByte()) {
                    throw java.io.IOException("invalid miIO response")
                }
                val length = ByteBuffer.wrap(responseBytes, 2, 2).order(ByteOrder.BIG_ENDIAN).short.toInt() and 0xffff
                if (length > responseBytes.size || length < 32) throw java.io.IOException("invalid miIO length $length")
                val responseHeader = responseBytes.copyOfRange(0, 16)
                val responseChecksum = responseBytes.copyOfRange(16, 32)
                val responseEncrypted = responseBytes.copyOfRange(32, length)
                val expected = md5(responseHeader + token + responseEncrypted)
                if (!MessageDigest.isEqual(responseChecksum, expected)) throw java.io.IOException("miIO checksum mismatch (token?)")

                val decryptor = Cipher.getInstance("AES/CBC/PKCS5Padding")
                decryptor.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), IvParameterSpec(iv))
                val decoded = decryptor.doFinal(responseEncrypted).toString(Charsets.UTF_8).trimEnd('\u0000')
                val json = JSONObject(decoded)
                json.optJSONObject("error")?.let { throw java.io.IOException("miIO error ${it.optInt("code")}: ${it.optString("message")}") }
                return json
            } catch (e: Exception) {
                last = e
            }
        }
        throw last ?: java.io.IOException("miIO request failed")
    }

    private fun hello(host: Inet4Address): MiioHello {
        val bytes = socket(1800).use { socket ->
            socket.send(DatagramPacket(HELLO, HELLO.size, host, 54321))
            val buf = ByteArray(1024)
            val response = DatagramPacket(buf, buf.size)
            socket.receive(response)
            buf.copyOf(response.length)
        }
        if (bytes.size < 32 || bytes[0] != 0x21.toByte() || bytes[1] != 0x31.toByte()) throw java.io.IOException("miIO hello failed")
        val bb = ByteBuffer.wrap(bytes).order(ByteOrder.BIG_ENDIAN)
        bb.position(8)
        val deviceId = bb.int.toLong() and 0xffffffffL
        val stamp = bb.int.toLong() and 0xffffffffL
        return MiioHello(deviceId, stamp)
    }

    private fun socket(timeoutMs: Int): DatagramSocket {
        val socket = DatagramSocket(null)
        socket.reuseAddress = true
        socket.bind(if (localAddress != null) InetSocketAddress(localAddress, 0) else InetSocketAddress(0))
        socket.soTimeout = timeoutMs
        return socket
    }

    private fun tokenBytes(tokenHex: String): ByteArray {
        val normalized = normalizeMiioToken(tokenHex) ?: throw IllegalArgumentException("miIO token must be 16 bytes / 32 hex")
        return ByteArray(16) { i -> normalized.substring(i * 2, i * 2 + 2).toInt(16).toByte() }
    }

    private fun md5(data: ByteArray): ByteArray = MessageDigest.getInstance("MD5").digest(data)

    companion object {
        val HELLO: ByteArray = byteArrayOf(0x21, 0x31, 0x00, 0x20) + ByteArray(28) { 0xff.toByte() }
    }
}
