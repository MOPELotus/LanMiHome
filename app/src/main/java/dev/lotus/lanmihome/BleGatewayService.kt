package dev.lotus.lanmihome

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.ParcelUuid
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

private const val CHANNEL_ID = "lanmihome_ble_gateway"
private const val NOTIFICATION_ID = 1103
private val MI_BEACON_UUID = ParcelUuid.fromString("0000fe95-0000-1000-8000-00805F9B34FB")

class BleGatewayService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var retryJob: Job? = null
    private var scannerActive = false
    private var lastRaw = ""
    private var lastFreshnessWrite = 0L
    private var uploadInFlight = false

    private val callback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) = handleResult(result)
        override fun onBatchScanResults(results: MutableList<ScanResult>) = results.forEach(::handleResult)
        override fun onScanFailed(errorCode: Int) {
            scannerActive = false
            prefs().edit().putBoolean("scanning", false).putString("last_error", "BLE scan failed: $errorCode").apply()
        }
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        val notification = buildNotification("等待温湿度计广播")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        prefs().edit().putBoolean("running", true).putString("last_error", null).apply()
        retryJob = scope.launch {
            while (isActive) {
                if (!scannerActive) startScan()
                delay(30_000)
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        prefs().edit().putBoolean("enabled", true).apply()
        if (!scannerActive) startScan()
        return START_STICKY
    }

    override fun onDestroy() {
        retryJob?.cancel()
        stopScan()
        prefs().edit().putBoolean("running", false).putBoolean("scanning", false).apply()
        scope.coroutineContext[Job]?.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    @SuppressLint("MissingPermission")
    private fun startScan() {
        if (checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) != PackageManager.PERMISSION_GRANTED) {
            prefs().edit().putString("last_error", "BLUETOOTH_SCAN 未授权").apply()
            return
        }
        val manager = getSystemService(BluetoothManager::class.java)
        val scanner = manager?.adapter?.bluetoothLeScanner
        if (scanner == null) {
            prefs().edit().putBoolean("scanning", false).putString("last_error", "蓝牙未开启或扫描器不可用").apply()
            return
        }
        runCatching { scanner.stopScan(callback) }
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_POWER)
            .setCallbackType(ScanSettings.CALLBACK_TYPE_ALL_MATCHES)
            .setReportDelay(0)
            .build()
        runCatching { scanner.startScan(null, settings, callback) }
            .onSuccess {
                scannerActive = true
                prefs().edit().putBoolean("scanning", true).putString("last_error", null).apply()
            }
            .onFailure {
                scannerActive = false
                prefs().edit().putBoolean("scanning", false).putString("last_error", it.message ?: it.javaClass.simpleName).apply()
            }
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        if (checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) != PackageManager.PERMISSION_GRANTED) return
        val scanner = getSystemService(BluetoothManager::class.java)?.adapter?.bluetoothLeScanner
        runCatching { scanner?.stopScan(callback) }
        scannerActive = false
    }

    @SuppressLint("MissingPermission")
    private fun handleResult(result: ScanResult) {
        val record = result.scanRecord ?: return
        val fe95 = record.getServiceData(MI_BEACON_UUID)
            ?: record.bytes?.let(MiBeaconV5::extractFe95)
            ?: return
        if (fe95.size < 4 || ((fe95[2].toInt() and 0xff) or ((fe95[3].toInt() and 0xff) shl 8)) != SENSOR_PRODUCT_ID) return

        val now = System.currentTimeMillis()
        val raw = fe95.hex()
        val p = prefs()
        val sensorPackets = p.getLong("sensor_packets", 0) + 1

        if (raw == lastRaw) {
            if (now - lastFreshnessWrite >= 10_000) {
                lastFreshnessWrite = now
                p.edit().putLong("sensor_packets", sensorPackets).putLong("last_seen_ms", now).putInt("rssi", result.rssi).apply()
            }
            return
        }
        lastRaw = raw
        lastFreshnessWrite = now

        val advertisedAddress = runCatching { result.device.address }.getOrNull()
        val frame = MiBeaconV5.decodeSensor(fe95, BleGateway.readBindKey(this), advertisedAddress) ?: return
        val baseEditor = p.edit()
            .putLong("sensor_packets", sensorPackets)
            .putLong("last_seen_ms", now)
            .putInt("rssi", result.rssi)
            .putString("mac", frame.mac)

        if (!frame.authenticated) {
            val frameControl = if (fe95.size >= 2) {
                (fe95[0].toInt() and 0xff) or ((fe95[1].toInt() and 0xff) shl 8)
            } else 0
            val counter = if (fe95.size >= 5) fe95[4].toInt() and 0xff else -1
            val encrypted = fe95.isNotEmpty() && (fe95[0].toInt() and 0x08) != 0
            val capability = fe95.isNotEmpty() && (fe95[0].toInt() and 0x20) != 0
            val compact = encrypted && fe95.size == 19
            val payloadStart = if (compact) 5 else if (capability) 12 else 11
            val meta = "len=${fe95.size} fc=0x%04X counter=%s encrypted=%s compact=%s capability=%s payloadStart=%d addr=%s rssi=%d".format(
                frameControl,
                if (counter >= 0) counter.toString() else "?",
                encrypted,
                compact,
                capability,
                payloadStart,
                advertisedAddress ?: "?",
                result.rssi,
            )
            baseEditor
                .putLong("decrypt_fail", p.getLong("decrypt_fail", 0) + 1)
                .putString("last_fail_raw", raw)
                .putString("last_fail_meta", meta)
                .putLong("last_fail_seen_ms", now)
                .apply()
            return
        }

        val editor = baseEditor
            .putLong("decrypt_ok", p.getLong("decrypt_ok", 0) + 1)
            .putString("last_raw", frame.raw)
        frame.plain?.let { editor.putString("last_plain", it) }
        var hasMeasurement = false
        frame.temperature?.let {
            editor.putLong("temperature", it.toBits()); hasMeasurement = true
        }
        frame.humidity?.let {
            editor.putLong("humidity", it.toBits()); hasMeasurement = true
        }
        frame.battery?.let {
            editor.putInt("battery", it); hasMeasurement = true
        }
        if (hasMeasurement) editor.putLong("last_measurement_ms", now)
        editor.apply()

        if (hasMeasurement) {
            updateNotification()
            upload(frame, result.rssi, now)
        }
    }

    private fun upload(frame: MiBeaconSensorFrame, rssi: Int, seenAt: Long) {
        if (uploadInFlight) return
        uploadInFlight = true
        scope.launch {
            try {
                val base = getSharedPreferences(MAIN_PREFS, Context.MODE_PRIVATE)
                    .getString("base_url", DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
                val report = SensorReport(
                    temperature = frame.temperature,
                    humidity = frame.humidity,
                    battery = frame.battery,
                    rssi = rssi,
                    mac = frame.mac,
                    frameCounter = frame.frameCounter,
                    raw = frame.raw,
                    seenAtMs = seenAt,
                )
                LanMiHomeApi(base).reportSensor(report)
                prefs().edit().putLong("last_upload_ms", System.currentTimeMillis()).putString("last_upload_error", null).apply()
            } catch (e: Exception) {
                prefs().edit().putString("last_upload_error", e.message ?: e.javaClass.simpleName).apply()
            } finally {
                uploadInFlight = false
            }
        }
    }

    private fun updateNotification() {
        val s = BleGateway.readState(this)
        val text = buildString {
            if (s.temperature != null) append("%.1f°C".format(s.temperature))
            if (s.humidity != null) {
                if (isNotEmpty()) append(" · ")
                append("%.1f%%".format(s.humidity))
            }
            if (isEmpty()) append("正在监听温湿度计")
        }
        getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, buildNotification(text))
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "LanMiHome BLE 网关", NotificationManager.IMPORTANCE_LOW).apply {
                    description = "常驻监听米家温湿度计 BLE 广播"
                    setShowBadge(false)
                }
            )
        }
    }

    private fun buildNotification(text: String): android.app.Notification {
        val launchIntent = packageManager.getLaunchIntentForPackage(packageName) ?: Intent(this, MainActivity::class.java)
        val pi = PendingIntent.getActivity(
            this, 0, launchIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return android.app.Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_gateway)
            .setContentTitle("LAN 米家 · BLE 网关")
            .setContentText(text)
            .setContentIntent(pi)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
    }

    private fun prefs() = getSharedPreferences(BLE_PREFS, Context.MODE_PRIVATE)
}
