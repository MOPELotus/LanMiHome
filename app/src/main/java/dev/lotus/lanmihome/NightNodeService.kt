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

private class NightHttpServer(private val port: Int, private val devices: NightDeviceManager) {
    private val running = AtomicBoolean(false)
    private val executor = Executors.newCachedThreadPool()
    private var server: ServerSocket? = null
    private var acceptThread: Thread? = null

    fun start() {
        if (!running.compareAndSet(false, true)) return
        server = ServerSocket().apply {
            reuseAddress = true
            bind(InetSocketAddress("0.0.0.0", port))
        }
        acceptThread = Thread({ acceptLoop() }, "lanmihome-night-http").apply { isDaemon = true; start() }
        NightNodeRuntime.log("夜间 HTTP 服务已监听 0.0.0.0:$port")
    }

    fun stop() {
        running.set(false)
        runCatching { server?.close() }
        executor.shutdownNow()
    }

    private fun acceptLoop() {
        while (running.get()) {
            try {
                val socket = server?.accept() ?: break
                executor.execute { handle(socket) }
            } catch (e: Exception) {
                if (running.get()) NightNodeRuntime.log("HTTP accept: ${e.message}")
            }
        }
    }

    private fun handle(socket: Socket) = socket.use { client ->
        client.soTimeout = 10000
        val reader = BufferedReader(InputStreamReader(client.getInputStream(), Charsets.UTF_8))
        val requestLine = reader.readLine() ?: return@use
        val parts = requestLine.split(' ')
        if (parts.size < 2) return@use
        val method = parts[0].uppercase()
        val path = parts[1].substringBefore('?').trimEnd('/').ifBlank { "/" }
        var contentLength = 0
        while (true) {
            val line = reader.readLine() ?: break
            if (line.isEmpty()) break
            val index = line.indexOf(':')
            if (index > 0 && line.substring(0, index).equals("Content-Length", true)) {
                contentLength = line.substring(index + 1).trim().toIntOrNull() ?: 0
            }
        }
        val bodyText = if (contentLength > 0) {
            val chars = CharArray(contentLength)
            var offset = 0
            while (offset < contentLength) {
                val n = reader.read(chars, offset, contentLength - offset)
                if (n <= 0) break
                offset += n
            }
            String(chars, 0, offset)
        } else "{}"
        val body = runCatching { JSONObject(bodyText) }.getOrElse { JSONObject() }

        try {
            val payload = route(method, path, body)
            respond(client, 200, payload)
        } catch (e: IllegalArgumentException) {
            respond(client, 400, JSONObject().put("error", e.message ?: "bad request"))
        } catch (e: Exception) {
            respond(client, 503, JSONObject().put("error", "${e.javaClass.simpleName}: ${e.message}"))
        }
    }

    private fun route(method: String, path: String, body: JSONObject): JSONObject {
        if (method == "GET") return when (path) {
            "/", "/api/v1/health" -> JSONObject()
                .put("ok", true).put("service", "lanmihome-night").put("version", 1)
                .put("fan", NightNodeRuntime.snapshot().fanIp != null)
                .put("lamp", NightNodeRuntime.snapshot().lampIp != null)
            "/api/v1/capabilities" -> JSONObject().put("api", "v1").put("fan", true).put("lamp", true)
                .put("sensor", false).put("chargers", JSONArray()).put("features", JSONArray().put("night-node").put("miot"))
            "/api/v1/fan" -> devices.fanState()
            "/api/v1/lamp" -> devices.lampState()
            "/api/v1/sensor" -> JSONObject().put("available", false).put("reports", 0)
            "/api/v1/chargers" -> JSONObject().put("chargers", JSONArray())
            "/api/v1/system/recovery" -> recoveryStub()
            "/api/v1/state" -> JSONObject().put("fan", devices.fanState()).put("lamp", devices.lampState())
                .put("sensor", JSONObject().put("available", false)).put("chargers", JSONArray()).put("recovery", recoveryStub())
            else -> throw IllegalArgumentException("not found")
        }
        if (method == "PATCH") return when (path) {
            "/api/v1/fan" -> devices.patchFan(body)
            "/api/v1/lamp" -> devices.patchLamp(body)
            else -> throw IllegalArgumentException("not found")
        }
        if (method == "POST") return when (path) {
            "/api/v1/fan/action" -> devices.fanAction(body)
            "/api/v1/lamp/action" -> devices.lampAction(body)
            "/api/v1/system/recovery/start" -> recoveryStub()
            else -> throw IllegalArgumentException("not found")
        }
        throw IllegalArgumentException("unsupported method")
    }

    private fun recoveryStub() = JSONObject().put("active", false).put("success", false).put("attempts", 0)
        .put("reason", "night-node").put("last_error", JSONObject.NULL)

    private fun respond(socket: Socket, status: Int, payload: JSONObject) {
        val data = payload.toString().toByteArray(Charsets.UTF_8)
        val reason = when (status) { 200 -> "OK"; 400 -> "Bad Request"; 503 -> "Service Unavailable"; else -> "Error" }
        val out = socket.getOutputStream()
        out.write("HTTP/1.1 $status $reason\r\n".toByteArray())
        out.write("Content-Type: application/json; charset=utf-8\r\n".toByteArray())
        out.write("Content-Length: ${data.size}\r\n".toByteArray())
        out.write("Cache-Control: no-store\r\nConnection: close\r\n\r\n".toByteArray())
        out.write(data)
        out.flush()
    }
}

class NightNodeService : Service() {
    private var worker: Thread? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private val stopping = AtomicBoolean(false)
    private lateinit var config: NightNodeConfig
    private lateinit var devices: NightDeviceManager
    private var http: NightHttpServer? = null

    override fun onCreate() {
        super.onCreate()
        val channel = NotificationChannel(NIGHT_CHANNEL, "LAN 米家夜间节点", NotificationManager.IMPORTANCE_LOW)
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        startForeground(NIGHT_NOTIFICATION_ID, notification("正在启动"))
        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "LanMiHome:NightNode")
            .apply { setReferenceCounted(false); acquire() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (worker?.isAlive == true) {
            if (intent?.action == ACTION_DISCOVER) NightNodeRuntime.requestDiscovery()
            return START_STICKY
        }
        config = NightNodePrefs.read(this)
        devices = NightDeviceManager(config)
        stopping.set(false)
        NightNodeRuntime.update { it.copy(running = true, hotspotManaged = config.manageHotspot, serverPort = config.port, lastError = null) }
        NightNodeRuntime.log("Night Node 启动")
        try {
            http = NightHttpServer(config.port, devices).also { it.start() }
        } catch (e: Exception) {
            NightNodeRuntime.log("HTTP 服务启动失败：${e.message}")
            NightNodeRuntime.update { it.copy(lastError = "HTTP: ${e.message}") }
        }
        worker = Thread({ runLoop() }, "lanmihome-night-worker").apply { isDaemon = true; start() }
        return START_STICKY
    }

    private fun runLoop() {
        var lastDiscovery = 0L
        var lastRootCheck = 0L
        var root = false
        while (!stopping.get()) {
            try {
                val loopNow = SystemClock.elapsedRealtime()
                if (!root || loopNow - lastRootCheck >= 60000) {
                    root = RootShell.available()
                    lastRootCheck = loopNow
                    NightNodeRuntime.update { it.copy(root = root) }
                }
                if (!root) {
                    NightNodeRuntime.update { it.copy(lastError = "未取得 root；无法管理热点/读取邻居表") }
                    SystemClock.sleep(5000)
                    continue
                }

                val info = NightNetwork.detect(config.interfaceName) ?: NightNetwork.startHotspot(config)
                devices.setHotspotInfo(info)
                NightNodeRuntime.update {
                    it.copy(
                        hotspotInterface = info?.interfaceName,
                        hotspotAddress = info?.address?.hostAddress,
                        lastError = if (info == null) "未发现热点接口" else null,
                    )
                }
                if (info != null) {
                    val now = SystemClock.elapsedRealtime()
                    val force = NightNodeRuntime.consumeDiscoveryRequest()
                    if (force || now - lastDiscovery >= 30000) {
                        devices.discover(force)
                        lastDiscovery = now
                    }
                }
                updateNotification()
            } catch (e: Exception) {
                NightNodeRuntime.log("worker: ${e.javaClass.simpleName}: ${e.message}")
                NightNodeRuntime.update { it.copy(lastError = "${e.javaClass.simpleName}: ${e.message}") }
            }
            SystemClock.sleep(2500)
        }
    }

    private fun notification(detail: String): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val pending = PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return Notification.Builder(this, NIGHT_CHANNEL)
            .setSmallIcon(android.R.drawable.stat_sys_wifi)
            .setContentTitle("LAN 米家 · Night Node")
            .setContentText(detail)
            .setContentIntent(pending)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification() {
        val s = NightNodeRuntime.snapshot()
        val detail = buildString {
            append(s.hotspotAddress ?: "等待热点")
            if (s.fanIp != null) append(" · 风扇在线")
            if (s.lampIp != null) append(" · 台灯在线")
        }
        getSystemService(NotificationManager::class.java).notify(NIGHT_NOTIFICATION_ID, notification(detail))
    }

    override fun onDestroy() {
        stopping.set(true)
        worker?.join(1500)
        http?.stop()
        if (::config.isInitialized) NightNetwork.stopHotspot(config)
        runCatching { wakeLock?.takeIf { it.isHeld }?.release() }
        wakeLock = null
        NightNodeRuntime.update { it.copy(running = false, hotspotInterface = null, hotspotAddress = null, fanIp = null, lampIp = null) }
        NightNodeRuntime.log("Night Node 已停止")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val ACTION_DISCOVER = "dev.lotus.lanmihome.action.NIGHT_DISCOVER"

        fun start(context: Context) {
            val intent = Intent(context, NightNodeService::class.java)
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, NightNodeService::class.java))
        }

        fun restart(context: Context) {
            stop(context)
            Handler(Looper.getMainLooper()).postDelayed({ start(context) }, 800)
        }

        fun discover(context: Context) {
            val intent = Intent(context, NightNodeService::class.java).setAction(ACTION_DISCOVER)
            context.startForegroundService(intent)
        }
    }
}
