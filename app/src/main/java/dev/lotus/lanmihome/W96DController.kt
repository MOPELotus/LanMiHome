package dev.lotus.lanmihome

import android.content.Context

internal class W96DController(private val context: Context) {
    private val ble = W96DBleClient.get(context)
    @Volatile private var activeBackend: W96DBackendTarget? = null

    suspend fun refresh(activeBase: String, primaryBase: String): W96DState {
        var last: Exception? = null
        for (target in W96DTargets.build(context, activeBase, primaryBase)) {
            try {
                val state = W96DBackendApi(target).state()
                if (state.available || state.connected) {
                    activeBackend = target
                    if (ble.isConnected && state.source == "router-bluez") ble.disconnect()
                    return state
                }
                last = ApiException(state.error ?: "W96D sidecar 不可用")
            } catch (e: Exception) { last = e }
        }
        activeBackend = null
        if (ble.isConnected) return ble.state()
        if (ble.hasRuntimePermissions()) {
            runCatching { ble.connect(scanIfNeeded = false) }.getOrNull()?.let { return it }
        }
        return W96DState(false, source = "none", error = last?.message ?: "未发现 W96D 服务端；可连接本机蓝牙")
    }

    suspend fun patch(activeBase: String, primaryBase: String, values: Map<String, Any>): W96DState {
        activeBackend?.let { runCatching { W96DBackendApi(it).patch(values) }.getOrNull()?.let { s -> return s } }
        for (target in W96DTargets.build(context, activeBase, primaryBase)) {
            runCatching { W96DBackendApi(target).patch(values) }.getOrNull()?.let { state -> activeBackend = target; return state }
        }
        if (ble.isConnected) return ble.patch(values)
        throw ApiException("没有可用的 W96D 服务端；请先连接本机蓝牙")
    }

    suspend fun connectDirect() = ble.connect(scanIfNeeded = true).also { activeBackend = null }
    suspend fun disconnectDirect() = ble.disconnect()
}
