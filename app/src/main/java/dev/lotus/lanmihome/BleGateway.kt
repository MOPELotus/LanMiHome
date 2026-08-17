package dev.lotus.lanmihome

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build

internal const val BLE_PREFS = "lanmihome_ble"
internal const val MAIN_PREFS = "lanmihome"
internal const val DEFAULT_SERVER_URL = "http://10.0.0.1:8765"

data class BleGatewayState(
    val enabled: Boolean = false,
    val running: Boolean = false,
    val scanning: Boolean = false,
    val bindKeyConfigured: Boolean = false,
    val sensorPackets: Long = 0,
    val decryptOk: Long = 0,
    val decryptFail: Long = 0,
    val temperature: Double? = null,
    val humidity: Double? = null,
    val battery: Int? = null,
    val mac: String? = null,
    val rssi: Int? = null,
    val lastSeenMs: Long = 0,
    val lastMeasurementMs: Long = 0,
    val lastRaw: String = "",
    val lastPlain: String = "",
    val lastFailRaw: String = "",
    val lastFailMeta: String = "",
    val lastFailSeenMs: Long = 0,
    val lastUploadMs: Long = 0,
    val lastUploadError: String? = null,
    val lastError: String? = null,
)

object BleGateway {
    fun requiredPermissions(): Array<String> = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        arrayOf(
            Manifest.permission.BLUETOOTH_SCAN,
            Manifest.permission.ACCESS_COARSE_LOCATION,
            Manifest.permission.ACCESS_FINE_LOCATION,
        )
    } else {
        arrayOf(Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.ACCESS_FINE_LOCATION)
    }

    fun notificationPermission(): String? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        Manifest.permission.POST_NOTIFICATIONS
    } else null

    fun hasPermissions(context: Context): Boolean = requiredPermissions().all {
        context.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED
    }

    fun readState(context: Context): BleGatewayState {
        val p = context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE)
        return BleGatewayState(
            enabled = p.getBoolean("enabled", false),
            running = p.getBoolean("running", false),
            scanning = p.getBoolean("scanning", false),
            bindKeyConfigured = readBindKey(context) != null,
            sensorPackets = p.getLong("sensor_packets", 0),
            decryptOk = p.getLong("decrypt_ok", 0),
            decryptFail = p.getLong("decrypt_fail", 0),
            temperature = if (p.contains("temperature")) Double.fromBits(p.getLong("temperature", 0)) else null,
            humidity = if (p.contains("humidity")) Double.fromBits(p.getLong("humidity", 0)) else null,
            battery = if (p.contains("battery")) p.getInt("battery", 0) else null,
            mac = p.getString("mac", null),
            rssi = if (p.contains("rssi")) p.getInt("rssi", 0) else null,
            lastSeenMs = p.getLong("last_seen_ms", 0),
            lastMeasurementMs = p.getLong("last_measurement_ms", 0),
            lastRaw = p.getString("last_raw", "") ?: "",
            lastPlain = p.getString("last_plain", "") ?: "",
            lastFailRaw = p.getString("last_fail_raw", "") ?: "",
            lastFailMeta = p.getString("last_fail_meta", "") ?: "",
            lastFailSeenMs = p.getLong("last_fail_seen_ms", 0),
            lastUploadMs = p.getLong("last_upload_ms", 0),
            lastUploadError = p.getString("last_upload_error", null),
            lastError = p.getString("last_error", null),
        )
    }

    fun normalizeBindKey(raw: String): String? {
        val clean = raw.filter { it.isDigit() || it.lowercaseChar() in 'a'..'f' }.lowercase()
        return clean.takeIf { it.length == 32 && it.all { c -> c.isDigit() || c in 'a'..'f' } }
    }

    fun saveBindKey(context: Context, raw: String): Boolean {
        val normalized = normalizeBindKey(raw) ?: return false
        context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE).edit()
            .putString("bindkey", normalized)
            .apply()
        return true
    }

    fun clearBindKey(context: Context) {
        context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE).edit().remove("bindkey").apply()
    }

    fun readBindKey(context: Context): ByteArray? {
        val s = context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE)
            .getString("bindkey", null) ?: return null
        if (s.length != 32) return null
        return runCatching { ByteArray(16) { i -> s.substring(i * 2, i * 2 + 2).toInt(16).toByte() } }.getOrNull()
    }

    fun start(context: Context): String? {
        if (!hasPermissions(context)) return "缺少蓝牙扫描/定位权限"
        context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE).edit().putBoolean("enabled", true).apply()
        val intent = Intent(context, BleGatewayService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent)
        else context.startService(intent)
        return null
    }

    fun stop(context: Context) {
        context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE).edit().putBoolean("enabled", false).apply()
        context.stopService(Intent(context, BleGatewayService::class.java))
    }

    fun clearStats(context: Context) {
        context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE).edit()
            .remove("sensor_packets")
            .remove("decrypt_ok")
            .remove("decrypt_fail")
            .remove("last_raw")
            .remove("last_plain")
            .remove("last_fail_raw")
            .remove("last_fail_meta")
            .remove("last_fail_seen_ms")
            .remove("last_error")
            .remove("last_upload_error")
            .apply()
    }
}
