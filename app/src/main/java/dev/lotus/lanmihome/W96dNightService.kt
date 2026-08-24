package dev.lotus.lanmihome

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.os.PowerManager
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.time.LocalTime
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking

private const val W96D_NIGHT_CHANNEL = "lanmihome-w96d-night"
private const val W96D_NIGHT_NOTIFICATION_ID = 8796
private const val W96D_NIGHT_PORT = 8766

internal object W96dNightRuntime {
    private val lock = Any()
    private var state = W96dState(owner = "night_node")
    fun get(): W96dState = synchronized(lock) { state.copy() }
    fun set(value: W96dState) = synchronized(lock) { state = value.copy(owner = "night_node") }
    fun update(block: (W96dState) -> W96dState) = synchronized(lock) {
        state = block(state).copy(owner = "night_node")
    }
}

private class W96dNightHttpServer(
    private val port: Int,
    private val context: Context,
    private val ble: W96dGattClient,
) {
    private val running = AtomicBoolean(false)
    private val pool = Executors.newCachedThreadPool()
    private var socket: ServerSocket? = null

    fun start() {
        if (!running.compareAndSet(false, true)) return
        socket = ServerSocket().apply {
            reuseAddress = true
            bind(InetSocketAddress("0.0.0.0", port))
        }
        Thread({ acceptLoop() }, "w96d-night-http").apply { isDaemon = true; start() }
    }

    fun stop() {
        running.set(false)
        runCatching { socket?.close() }
        pool.shutdownNow()
    }

    private fun acceptLoop() {
        while (running.get()) {
            try {
                val client = socket?.accept() ?: break
                pool.execute { handle(client) }
            } catch (_: Exception) {
                if (!running.get()) break
            }
        }
    }

    private fun handle(client: Socket) = client.use { s ->
        s.soTimeout = 12_000
        val reader = BufferedReader(InputStreamReader(s.getInputStream(), Charsets.UTF_8))
        val requestLine = reader.readLine() ?: return@use
        val parts = requestLine.split(' ')
        if (parts.size < 2) return@use
        val method = parts[0].uppercase()
        val path = parts[1].substringBefore('?').trimEnd('/').ifBlank { "/" }
        var contentLength = 0
        while (true) {
            val line = reader.readLine() ?: break
            if (line.isEmpty()) break
            val i = line.indexOf(':')
            if (i > 0 && line.substring(0, i).equals("Content-Length", true)) {
                contentLength = line.substring(i + 1).trim().toIntOrNull() ?: 0
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
            respond(s, 200, route(method, path, body))
        } catch (e: IllegalArgumentException) {
            respond(s, 400, JSONObject().put("error", e.message ?: "bad request"))
        } catch (e: Exception) {
            respond(s, 503, JSONObject().put("error", "${e.javaClass.simpleName}: ${e.message}"))
        }
    }

    private fun route(method: String, path: String, body: JSONObject): JSONObject {
        if (method == "GET") return when (path) {
            "/", "/api/v1/health" -> JSONObject()
                .put("ok", true).put("service", "lanmihome-w96d-night").put("version", 1)
                .put("w96d", stateJson(W96dNightRuntime.get()))
            "/api/v1/w96d" -> stateJson(refreshIfOwned())
            "/api/v1/w96d/ownership" -> ownershipJson(W96dNightRuntime.get())
            else -> throw IllegalArgumentException("not found")
        }
        if (method == "PATCH" && path == "/api/v1/w96d") {
            requireOwned()
            val keys = body.keys().asSequence().toList()
            val allowed = setOf("power", "speed", "natural", "turbo", "indicator")
            val unknown = keys.filter { it !in allowed }
            require(unknown.isEmpty()) { "unknown field(s): ${unknown.joinToString()}" }
            var state = W96dNightRuntime.get()
            runBlocking {
                ble.connect()
                keys.forEach { key ->
                    val value: Any = when (key) {
                        "power", "natural", "turbo", "indicator" -> body.getBoolean(key)
                        "speed" -> body.getInt(key).also { require(it in 0..100) { "speed must be 0..100" } }
                        else -> error(key)
                    }
                    state = ble.patch(key, value).asNightState()
                }
            }
            W96dNightRuntime.set(state)
            return stateJson(state)
        }
        if (method == "POST" && path == "/api/v1/w96d/ownership") {
            return when (body.optString("state").lowercase()) {
                "release" -> {
                    W96dPrefs.setNodePaused(context, true)
                    runBlocking { ble.disconnect() }
                    val s = W96dNightRuntime.get().copy(
                        available = false,
                        connected = false,
                        paused = true,
                        scheduled = isNightWindow(),
                        error = null,
                    )
                    W96dNightRuntime.set(s)
                    ownershipJson(s).put("ok", true)
                }
                "resume" -> {
                    W96dPrefs.setNodePaused(context, false)
                    val s = W96dNightRuntime.get().copy(
                        paused = false,
                        scheduled = isNightWindow(),
                        error = null,
                    )
                    W96dNightRuntime.set(s)
                    ownershipJson(s).put("ok", true)
                }
                else -> throw IllegalArgumentException("state must be release or resume")
            }
        }
        throw IllegalArgumentException("not found")
    }

    private fun refreshIfOwned(): W96dState {
        val scheduled = isNightWindow()
        val paused = W96dPrefs.nodePaused(context)
        if (!scheduled || paused) {
            val s = W96dNightRuntime.get().copy(
                available = false,
                connected = false,
                scheduled = scheduled,
                paused = paused,
                error = if (!scheduled) {
                    "10S is outside 23:00-06:00 ownership window"
                } else {
                    "ownership released for outdoor mode"
                },
            )
            W96dNightRuntime.set(s)
            return s
        }
        val s = runBlocking {
            if (!hasW96dBlePermissions(context)) {
                throw IllegalStateException("BLE permission required on Night Node")
            }
            ble.connect().asNightState()
        }
        W96dNightRuntime.set(s)
        return s
    }

    private fun requireOwned() {
        require(isNightWindow()) { "10S is outside 23:00-06:00 ownership window" }
        require(!W96dPrefs.nodePaused(context)) { "W96D ownership is released for outdoor mode" }
    }

    private fun W96dState.asNightState() = copy(
        owner = "night_node",
        scheduled = isNightWindow(),
        paused = W96dPrefs.nodePaused(context),
        available = connected && isNightWindow() && !W96dPrefs.nodePaused(context),
    )

    private fun stateJson(s: W96dState): JSONObject = JSONObject().apply {
        put("available", s.available)
        put("connected", s.connected)
        put("name", s.name)
        putNullable("address", s.address)
        put("owner", "night_node")
        put("scheduled", s.scheduled)
        put("paused", s.paused)
        putNullable("power", s.power)
        putNullable("speed", s.speed)
        putNullable("natural", s.natural)
        putNullable("turbo", s.turbo)
        putNullable("turbo_remaining_seconds", s.turboRemainingSeconds)
        putNullable("indicator", s.indicator)
        putNullable("battery_voltage_mv", s.batteryVoltageMv)
        putNullable("battery_current_ma", s.batteryCurrentMa)
        putNullable("battery_capacity_mwh", s.batteryCapacityMwh)
        putNullable("vbus_voltage_mv", s.vbusVoltageMv)
        putNullable("charge_status", s.chargeStatus)
        putNullable("motor_current_ma", s.motorCurrentMa)
        putNullable("motor_voltage_mv", s.motorVoltageMv)
        putNullable("error", s.error)
        putNullable("updated_at", s.updatedAt)
    }

    private fun ownershipJson(s: W96dState) = JSONObject()
        .put("owner", "night_node")
        .put("scheduled", s.scheduled)
        .put("paused", s.paused)
        .put("connected", s.connected)

    private fun JSONObject.putNullable(key: String, value: Any?) {
        put(key, value ?: JSONObject.NULL)
    }

    private fun respond(socket: Socket, status: Int, payload: JSONObject) {
        val data = payload.toString().toByteArray(Charsets.UTF_8)
        val reason = when (status) {
            200 -> "OK"
            400 -> "Bad Request"
            503 -> "Service Unavailable"
            else -> "Error"
        }
        val out = socket.getOutputStream()
        out.write("HTTP/1.1 $status $reason\r\n".toByteArray())
        out.write("Content-Type: application/json; charset=utf-8\r\n".toByteArray())
        out.write("Content-Length: ${data.size}\r\n".toByteArray())
        out.write("Cache-Control: no-store\r\nConnection: close\r\n\r\n".toByteArray())
        out.write(data)
        out.flush()
    }
}

private fun isNightWindow(now: LocalTime = LocalTime.now()): Boolean =
    now >= LocalTime.of(23, 0) || now < LocalTime.of(6, 0)

class W96dNightService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private lateinit var ble: W96dGattClient
    private var http: W96dNightHttpServer? = null
    private var worker: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        val channel = NotificationChannel(
            W96D_NIGHT_CHANNEL,
            "LAN 米家 W96D 夜间节点",
            NotificationManager.IMPORTANCE_LOW,
        )
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        startForeground(
            W96D_NIGHT_NOTIFICATION_ID,
            notification("等待 23:00–06:00 控制窗口"),
        )
        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "LanMiHome:W96dNight")
            .apply {
                setReferenceCounted(false)
                acquire()
            }
        ble = W96dGattClient(this)
        http = W96dNightHttpServer(W96D_NIGHT_PORT, this, ble).also { it.start() }
        worker = scope.launch { runLoop() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    private suspend fun runLoop() {
        while (scope.isActive) {
            val scheduled = isNightWindow()
            val paused = W96dPrefs.nodePaused(this)
            try {
                val s = if (scheduled && !paused && hasW96dBlePermissions(this)) {
                    ble.connect().copy(
                        owner = "night_node",
                        scheduled = true,
                        paused = false,
                    )
                } else {
                    ble.disconnect()
                    W96dNightRuntime.get().copy(
                        available = false,
                        connected = false,
                        owner = "night_node",
                        scheduled = scheduled,
                        paused = paused,
                        error = if (!hasW96dBlePermissions(this)) {
                            "BLE permission required"
                        } else {
                            null
                        },
                    )
                }
                W96dNightRuntime.set(s)
            } catch (e: Exception) {
                W96dNightRuntime.update {
                    it.copy(
                        available = false,
                        connected = false,
                        scheduled = scheduled,
                        paused = paused,
                        error = "${e.javaClass.simpleName}: ${e.message}",
                    )
                }
            }
            val s = W96dNightRuntime.get()
            val detail = when {
                s.paused -> "已为外出模式释放 BLE"
                !s.scheduled -> "等待 23:00–06:00"
                s.connected -> "W96D 已连接"
                s.error != null -> s.error
                else -> "正在连接 W96D"
            } ?: "W96D"
            getSystemService(NotificationManager::class.java).notify(
                W96D_NIGHT_NOTIFICATION_ID,
                notification(detail),
            )
            delay(if (scheduled) 2_000 else 10_000)
        }
    }

    private fun notification(text: String): Notification {
        val pending = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return Notification.Builder(this, W96D_NIGHT_CHANNEL)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("LAN 米家 · W96D Night Owner")
            .setContentText(text)
            .setContentIntent(pending)
            .setOngoing(true)
            .build()
    }

    override fun onDestroy() {
        worker?.cancel()
        http?.stop()
        runBlocking { runCatching { ble.disconnect() } }
        runCatching { wakeLock?.takeIf { it.isHeld }?.release() }
        wakeLock = null
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        fun start(context: Context) {
            if (!BuildConfig.NIGHT_NODE_ENABLED) return
            context.startForegroundService(Intent(context, W96dNightService::class.java))
        }
    }
}
