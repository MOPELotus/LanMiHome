package dev.lotus.lanmihome

import androidx.compose.foundation.layout.Column
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable

/**
 * W96D control page foundation.
 *
 * Final navigation wiring intentionally stays with existing device routing.
 */
@Composable
fun W96DScreen(state: W96DState = W96DState()) {
    Column {
        Text("W96D New")
        Text("控制来源: ${state.owner}")
        Text("风速: ${state.speed}%")
        Text("电池: ${state.battery ?: "-"}")
        Text("VBUS: ${state.vbus ?: false}")
        Text("电机: ${state.motor ?: "unknown"}")
    }
}
