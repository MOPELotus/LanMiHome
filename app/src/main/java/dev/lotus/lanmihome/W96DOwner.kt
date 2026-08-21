package dev.lotus.lanmihome

/** BLE ownership shown in UI. */
enum class W96DOwner {
    ROUTER,
    NIGHT_NODE,
    PHONE,
}

fun ownerLabel(owner: String): String = when (owner.lowercase()) {
    "router" -> "路由器"
    "night_node" -> "10S 夜间节点"
    "phone" -> "本机蓝牙"
    else -> owner
}
