package dev.lotus.lanmihome

import android.content.Context
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

internal class W96DNodeServer(
    context: Context,
    private val port: Int = 8766,
) {
    private val ble = W96DBleClient.get(context)
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
        acceptThread = Thread({ acceptLoop() }, "lanmihome-w96d-http").apply {
            isDaemon = true
            start()
        }
        executor.execute {
            runCatching {
                runBlocking { ble.connect(scanIfNeeded = true) }
            }.onFailure {
                NightNodeRuntime.log("W96D 预连接失败：${it.message}")
            }
        }
        NightNodeRuntime.log("W96D HTTP 服务已监听 0.0.0.0:$port")
    }

    fun stop() {
        running.set(false)
        runCatching { server?.close() }
        executor.shutdownNow()
        runCatching { runBlocking { ble.disconnect() } }
    }

    private fun acceptLoop() {
        while (running.get()) {
            try {
                val socket = server?.accept() ?: break
                executor.execute { handle(socket) }
            } catch (e: Exception) {
                if (running.get()) NightNodeRuntime.log("W96D HTTP accept: ${e.message}")
            }
        }
    }

    private fun handle(socket: Socket) = socket.use { client ->
        client.soTimeout = 15_000
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
        } catch (e: SecurityException) {
            respond(client, 503, JSONObject().put("error", "需要先在 Night APK 中授予蓝牙权限"))
        } catch (e: Exception) {
            respond(client, 503, JSONObject().put("error", "${e.javaClass.simpleName}: ${e.message}"))
        }
    }

    private fun route(method: String, path: String, body: JSONObject): JSONObject = runBlocking {
        if (method == "GET") return@runBlocking when (path) {
            "/", "/api/v1/health" -> {
                val state = ble.state().copy(source = "night-node-ble")
                JSONObject()
                    .put("ok", true)
                    .put("service", "lanmihome-w96d")
                    .put("available", state.available)
                    .put("source", state.source)
            }
            "/api/v1/w96d", "/api/v1/state" -> ble.state().copy(source = "night-node-ble").toJson()
            else -> throw IllegalArgumentException("not found")
        }

        if (method == "PATCH") return@runBlocking when (path) {
            "/api/v1/w96d" -> ble.patch(jsonMap(body)).copy(source = "night-node-ble").toJson()
            else -> throw IllegalArgumentException("not found")
        }

        if (method == "POST") return@runBlocking when (path) {
            "/api/v1/w96d/action" -> {
                when (val name = body.optString("name")) {
                    "disconnect" -> ble.disconnect()
                    "reconnect" -> {
                        ble.disconnect()
                        ble.connect(scanIfNeeded = true)
                    }
                    else -> throw IllegalArgumentException("unknown W96D action: $name")
                }
                JSONObject().put("ok", true).put("action", body.optString("name"))
            }
            else -> throw IllegalArgumentException("not found")
        }

        throw IllegalArgumentException("unsupported method")
    }

    private fun jsonMap(body: JSONObject): Map<String, Any> {
        val out = linkedMapOf<String, Any>()
        body.keys().forEach { key ->
            val value = body.get(key)
            out[key] = when (value) {
                is JSONArray -> List(value.length()) { index -> value.get(index) }
                JSONObject.NULL -> throw IllegalArgumentException("$key cannot be null")
                else -> value
            }
        }
        return out
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
