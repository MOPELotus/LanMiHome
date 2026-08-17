package dev.lotus.lanmihome

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.ParcelUuid
import android.os.PowerManager
import android.os.SystemClock
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

private const val CHANNEL_ID = "lanmihome_ble_gateway"
private const val NOTIFICATION_ID = 1103
private const val WATCHDOG_INTERVAL_MS = 30_000L
private const val NO_PACKET_RESTART_MS = 120_000L
private val MI_BEACON_UUID = ParcelUuid.fromString("0000fe95-0000-1000-8000-00805F9B34FB")

class BleGatewayService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val scanMutex = Mutex()
    private var watchdogJob: Job? = null
    private var screenRestartJob: Job? = null

    @Volatile private var scannerActive = false
    @Volatile private var lastTargetPacketElapsed = 0L
    @Volatile private var lastScanStartElapsed = 0L

    private var lastRaw = ""
    private var lastFreshnessWrite = 0L
    private var uploadInFlight = false
    private var receiverRegistered = false
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    private val callback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) = handleResult(result)
        override fun onBatchScanResults(results: MutableList<ScanResult>) = results.forEach(::handleResult)
        override fun onScanFailed(errorCode: Int) {
            scannerActive = false
            prefs().edit()
                .putBoolean("scanning", false)
                .putString("last_error", "BLE scan failed: $errorCode")
                .apply()
        }
    }

    private val screenReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                Intent.ACTION_SCREEN_OFF -> {
                    prefs().edit().putString("last_keepalive_event", "screen_off").apply()
                    screenRestartJob?.cancel()
                    screenRestartJob = scope.launch {
                        delay(1_500)
                        restartScan("screen_off")
                        // Some Xiaomi Bluetooth stacks accept the first restart but still
                        // suppress callbacks after display suspend. Give it one extra kick.
                        delay(45_000)
                        val now = SystemClock.elapsedRealtime()
                        val lastUseful = maxOf(lastTargetPacketElapsed, lastScanStartElapsed)
                        if (now - lastUseful >= 40_000) restartScan("screen_off_second_kick")
                    }
                }
                Intent.ACTION_SCREEN_ON -> {
                    prefs().edit().putString("last_keepalive_event", "screen_on").apply()
                    scope.launch { restartScan("screen_on", 250) }
                }
                Intent.ACTION_POWER_CONNECTED -> {
                    prefs().edit().putString("last_keepalive_event", "power_connected").apply()
                    scope.launch { restartScan("power_connected", 250) }
                }
            }
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

        acquireKeepAliveLocks()
        registerKeepAliveReceiver()
        applyRootKeepAliveBestEffort()

        val elapsed = SystemClock.elapsedRealtime()
        lastTargetPacketElapsed = elapsed
        lastScanStartElapsed = elapsed
        prefs().edit()
            .putBoolean("running", true)
            .putString("last_error", null)
            .putBoolean("wake_lock", wakeLock?.isHeld == true)
            .putBoolean("wifi_lock", wifiLock?.isHeld == true)
            .apply()

        watchdogJob = scope.launch {
            restartScan("service_create", 200)
            while (isActive) {
                delay(WATCHDOG_INTERVAL_MS)
                val now = SystemClock.elapsedRealtime()
                val lastUseful = maxOf(lastTargetPacketElapsed, lastScanStartElapsed)
                when {
                    !scannerActive -> restartScan("watchdog_scanner_inactive", 500)
                    now - lastUseful >= NO_PACKET_RESTART_MS -> restartScan("watchdog_no_target_120s")
                }
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        prefs().edit().putBoolean("enabled", true).apply()
        if (!scannerActive) scope.launch { restartScan("start_command", 250) }
        return START_STICKY
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        // START_STICKY normally keeps the service alive after the task is removed.
        // Record this so the UI can distinguish a task swipe from a BLE watchdog event.
        prefs().edit().putString("last_keepalive_event", "task_removed").apply()
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        watchdogJob?.cancel()
        screenRestartJob?.cancel()
        stopScan()
        unregisterKeepAliveReceiver()
        releaseKeepAliveLocks()
        prefs().edit()
            .putBoolean("running", false)
            .putBoolean("scanning", false)
            .putBoolean("wake_lock", false)
            .putBoolean("wifi_lock", false)
            .apply()
        scope.coroutineContext[Job]?.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private suspend fun restartScan(reason: String, settleMs: Long = 1_000L) {
        scanMutex.withLock {
            stopScan()
            val p = prefs()
            p.edit()
                .putLong("scan_restarts", p.getLong("scan_restarts", 0) + 1)
                .putString("last_scan_restart_reason", reason)
                .putLong("last_scan_restart_ms", System.currentTimeMillis())
                .putString("last_keepalive_event", reason)
                .apply()
            if (settleMs > 0) delay(settleMs)
            startScan()
        }
    }

    @SuppressLint("MissingPermission")
    private fun startScan() {
        if (checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) != PackageManager.PERMISSION_GRANTED) {
            scannerActive = false
            prefs().edit().putBoolean("scanning", false).putString("last_error", "BLUETOOTH_SCAN 未授权").apply()
            return
        }
        val manager = getSystemService(BluetoothManager::class.java)
        val scanner = manager?.adapter?.bluetoothLeScanner
        if (scanner == null) {
            scannerActive = false
            prefs().edit()
                .putBoolean("scanning", false)
                .putString("last_error", "蓝牙未开启或扫描器不可用")
                .apply()
            return
        }

        // FE95 is Service Data (AD type 0x16). Keep a real filter so Android can
        // offload it and keep the scan eligible while the display is off.
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
            .setMatchMode(ScanSettings.MATCH_MODE_AGGRESSIVE)
            .setNumOfMatches(ScanSettings.MATCH_NUM_MAX_ADVERTISEMENT)
            .setReportDelay(0)
            .build()

        runCatching { scanner.startScan(filters, settings, callback) }
            .onSuccess {
                scannerActive = true
                lastScanStartElapsed = SystemClock.elapsedRealtime()
                prefs().edit()
                    .putBoolean("scanning", true)
                    .putString("last_error", null)
                    .apply()
            }
            .onFailure {
                scannerActive = false
                prefs().edit()
                    .putBoolean("scanning", false)
                    .putString("last_error", it.message ?: it.javaClass.simpleName)
                    .apply()
            }
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        if (checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED) {
            val scanner = getSystemService(BluetoothManager::class.java)?.adapter?.bluetoothLeScanner
            runCatching { scanner?.stopScan(callback) }
        }
        scannerActive = false
        prefs().edit().putBoolean("scanning", false).apply()
    }

    @SuppressLint("MissingPermission")
    private fun handleResult(result: ScanResult) {
        val record = result.scanRecord ?: return
        val fe95 = record.getServiceData(MI_BEACON_UUID)
            ?: record.bytes?.let(MiBeaconV5::extractFe95)
            ?: return
        if (fe95.size < 4 || ((fe95[2].toInt() and 0xff) or ((fe95[3].toInt() and 0xff) shl 8)) != SENSOR_PRODUCT_ID) return

        lastTargetPacketElapsed = SystemClock.elapsedRealtime()
        val now = System.currentTimeMillis()
        val raw = fe95.hex()
        val p = prefs()
        val sensorPackets = p.getLong("sensor_packets", 0) + 1

        if (raw == lastRaw) {
            if (now - lastFreshnessWrite >= 10_000) {
                lastFreshnessWrite = now
                p.edit()
                    .putLong("sensor_packets", sensorPackets)
                    .putLong("last_seen_ms", now)
                    .putInt("rssi", result.rssi)
                    .apply()
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
            editor.putLong("temperature", it.toBits())
            hasMeasurement = true
        }
        frame.humidity?.let {
            editor.putLong("humidity", it.toBits())
            hasMeasurement = true
        }
        frame.battery?.let {
            editor.putInt("battery", it)
            hasMeasurement = true
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
                prefs().edit()
                    .putLong("last_upload_ms", System.currentTimeMillis())
                    .putString("last_upload_error", null)
                    .apply()
            } catch (e: Exception) {
                prefs().edit()
                    .putString("last_upload_error", e.message ?: e.javaClass.simpleName)
                    .apply()
            } finally {
                uploadInFlight = false
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun acquireKeepAliveLocks() {
        runCatching {
            val pm = getSystemService(PowerManager::class.java)
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "$packageName:BleGateway").apply {
                setReferenceCounted(false)
                acquire()
            }
        }
        runCatching {
            val wm = applicationContext.getSystemService(WifiManager::class.java)
            wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "$packageName:BleGatewayWifi").apply {
                setReferenceCounted(false)
                acquire()
            }
        }
    }

    private fun releaseKeepAliveLocks() {
        runCatching { if (wakeLock?.isHeld == true) wakeLock?.release() }
        runCatching { if (wifiLock?.isHeld == true) wifiLock?.release() }
        wakeLock = null
        wifiLock = null
    }

    private fun registerKeepAliveReceiver() {
        val filter = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_OFF)
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_POWER_CONNECTED)
        }
        runCatching {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                registerReceiver(screenReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
            } else {
                @Suppress("DEPRECATION")
                registerReceiver(screenReceiver, filter)
            }
            receiverRegistered = true
        }
    }

    private fun unregisterKeepAliveReceiver() {
        if (!receiverRegistered) return
        runCatching { unregisterReceiver(screenReceiver) }
        receiverRegistered = false
    }

    private fun applyRootKeepAliveBestEffort() {
        scope.launch {
            val pkg = packageName
            val command = listOf(
                "cmd deviceidle whitelist +$pkg",
                "cmd appops set $pkg RUN_ANY_IN_BACKGROUND allow",
                "am set-standby-bucket $pkg active",
                "am set-inactive $pkg false",
            ).joinToString("; ")
            val status = runCatching {
                val process = ProcessBuilder("su", "-c", command)
                    .redirectErrorStream(true)
                    .start()
                val output = process.inputStream.bufferedReader().readText().trim().take(500)
                val exit = process.waitFor()
                if (output.isBlank()) "exit=$exit" else "exit=$exit $output"
            }.getOrElse { "unavailable: ${it.message ?: it.javaClass.simpleName}" }
            prefs().edit()
                .putString("root_keepalive_status", status)
                .putLong("root_keepalive_ms", System.currentTimeMillis())
                .apply()
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
            this,
            0,
            launchIntent,
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
