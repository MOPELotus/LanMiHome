package dev.lotus.lanmihome

import android.Manifest
import android.annotation.SuppressLint
import android.app.PendingIntent
import android.bluetooth.BluetoothManager
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.ParcelUuid

private const val BLE_PREFS = "lanmihome_ble"
private const val ACTION_BLE_SCAN = "dev.lotus.lanmihome.BLE_SCAN"
private const val REQUEST_CODE_BLE_SCAN = 6201
private const val PRODUCT_ID_MJWSD06MMC = 21941

private val MI_BEACON_UUID = ParcelUuid.fromString("0000fe95-0000-1000-8000-00805f9b34fb")

data class BleDiagState(
    val enabled: Boolean = false,
    val totalPackets: Long = 0,
    val sensorPackets: Long = 0,
    val lastSeenMs: Long = 0,
    val lastRssi: Int? = null,
    val lastProductId: Int? = null,
    val lastRaw: String = "",
    val sensorLastSeenMs: Long = 0,
    val sensorLastRssi: Int? = null,
    val sensorLastRaw: String = "",
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

    fun hasPermissions(context: Context): Boolean = requiredPermissions().all {
        context.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED
    }

    fun readState(context: Context): BleDiagState {
        val p = context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE)
        return BleDiagState(
            enabled = p.getBoolean("enabled", false),
            totalPackets = p.getLong("total_packets", 0),
            sensorPackets = p.getLong("sensor_packets", 0),
            lastSeenMs = p.getLong("last_seen_ms", 0),
            lastRssi = if (p.contains("last_rssi")) p.getInt("last_rssi", 0) else null,
            lastProductId = if (p.contains("last_product_id")) p.getInt("last_product_id", 0) else null,
            lastRaw = p.getString("last_raw", "") ?: "",
            sensorLastSeenMs = p.getLong("sensor_last_seen_ms", 0),
            sensorLastRssi = if (p.contains("sensor_last_rssi")) p.getInt("sensor_last_rssi", 0) else null,
            sensorLastRaw = p.getString("sensor_last_raw", "") ?: "",
            lastError = p.getString("last_error", null),
        )
    }

    fun clear(context: Context) {
        val p = context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE)
        p.edit()
            .remove("total_packets")
            .remove("sensor_packets")
            .remove("last_seen_ms")
            .remove("last_rssi")
            .remove("last_product_id")
            .remove("last_raw")
            .remove("sensor_last_seen_ms")
            .remove("sensor_last_rssi")
            .remove("sensor_last_raw")
            .remove("last_error")
            .apply()
    }

    private fun pendingIntent(context: Context): PendingIntent {
        val intent = Intent(context, BleScanReceiver::class.java).setAction(ACTION_BLE_SCAN)
        return PendingIntent.getBroadcast(
            context,
            REQUEST_CODE_BLE_SCAN,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    @SuppressLint("MissingPermission")
    fun start(context: Context): String? {
        if (!hasPermissions(context)) return "缺少蓝牙扫描/定位权限"
        val manager = context.getSystemService(BluetoothManager::class.java)
            ?: return "设备没有 BluetoothManager"
        val adapter = manager.adapter ?: return "设备不支持蓝牙"
        val scanner = adapter.bluetoothLeScanner ?: return "蓝牙未开启或 BLE 扫描不可用"

        // Match any advertisement that contains FE95 service data. Do not use a
        // zero-length serviceData value here: vendor BLE stacks differ in how they
        // interpret it. A one-byte, all-zero mask means the FE95 field must exist,
        // while no payload bit is required to equal a specific value.
        val filters = listOf(
            ScanFilter.Builder()
                .setServiceData(
                    MI_BEACON_UUID,
                    byteArrayOf(0),
                    byteArrayOf(0),
                )
                .build()
        )
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .setCallbackType(ScanSettings.CALLBACK_TYPE_ALL_MATCHES)
            .setReportDelay(0)
            .build()

        val result = runCatching { scanner.startScan(filters, settings, pendingIntent(context)) }
        val error = result.exceptionOrNull()?.message
            ?: result.getOrNull()?.takeIf { it != 0 }?.let { "BLE startScan error=$it" }

        context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE).edit()
            .putBoolean("enabled", error == null)
            .putString("last_error", error)
            .apply()
        return error
    }

    @SuppressLint("MissingPermission")
    fun stop(context: Context): String? {
        if (!hasPermissions(context)) return "缺少蓝牙扫描/定位权限"
        val manager = context.getSystemService(BluetoothManager::class.java)
        val scanner = manager?.adapter?.bluetoothLeScanner
        val error = runCatching { scanner?.stopScan(pendingIntent(context)) }
            .exceptionOrNull()?.message
        context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE).edit()
            .putBoolean("enabled", false)
            .putString("last_error", error)
            .apply()
        return error
    }

    internal fun onResults(context: Context, results: List<ScanResult>, errorCode: Int?) {
        val p = context.getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE)
        var total = p.getLong("total_packets", 0)
        var sensor = p.getLong("sensor_packets", 0)
        var lastSeen = p.getLong("last_seen_ms", 0)
        var lastRssi: Int? = if (p.contains("last_rssi")) p.getInt("last_rssi", 0) else null
        var lastProductId: Int? = if (p.contains("last_product_id")) p.getInt("last_product_id", 0) else null
        var lastRaw = p.getString("last_raw", "") ?: ""
        var sensorLastSeen = p.getLong("sensor_last_seen_ms", 0)
        var sensorLastRssi: Int? = if (p.contains("sensor_last_rssi")) p.getInt("sensor_last_rssi", 0) else null
        var sensorLastRaw = p.getString("sensor_last_raw", "") ?: ""

        for (result in results) {
            val data = result.scanRecord?.getServiceData(MI_BEACON_UUID) ?: continue
            val now = System.currentTimeMillis()
            total++
            val productId = if (data.size >= 4) {
                (data[2].toInt() and 0xff) or ((data[3].toInt() and 0xff) shl 8)
            } else null
            val raw = data.joinToString("") { "%02x".format(it.toInt() and 0xff) }
            if (productId == PRODUCT_ID_MJWSD06MMC) {
                sensor++
                sensorLastSeen = now
                sensorLastRssi = result.rssi
                sensorLastRaw = raw
            }
            lastSeen = now
            lastRssi = result.rssi
            lastProductId = productId
            lastRaw = raw
        }

        p.edit()
            .putLong("total_packets", total)
            .putLong("sensor_packets", sensor)
            .putLong("last_seen_ms", lastSeen)
            .putLong("sensor_last_seen_ms", sensorLastSeen)
            .apply {
                if (lastRssi != null) putInt("last_rssi", lastRssi)
                if (lastProductId != null) putInt("last_product_id", lastProductId)
                if (sensorLastRssi != null) putInt("sensor_last_rssi", sensorLastRssi)
            }
            .putString("last_raw", lastRaw)
            .putString("sensor_last_raw", sensorLastRaw)
            .putString("last_error", errorCode?.takeIf { it != 0 }?.let { "BLE callback error=$it" })
            .commit()
    }
}

class BleScanReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_BLE_SCAN) return
        val results: ArrayList<ScanResult>? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableArrayListExtra(BluetoothLeScanner.EXTRA_LIST_SCAN_RESULT, ScanResult::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent.getParcelableArrayListExtra(BluetoothLeScanner.EXTRA_LIST_SCAN_RESULT)
        }
        val error = if (intent.hasExtra(BluetoothLeScanner.EXTRA_ERROR_CODE)) {
            intent.getIntExtra(BluetoothLeScanner.EXTRA_ERROR_CODE, 0)
        } else null
        BleGateway.onResults(context, results.orEmpty(), error)
    }
}
