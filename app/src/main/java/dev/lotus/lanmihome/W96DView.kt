package dev.lotus.lanmihome

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

@Composable
fun W96DView(
    state: W96DState,
    onPower: () -> Unit,
    onSpeed: (Int) -> Unit,
    onTurbo: () -> Unit,
    onNatural: () -> Unit,
    onLight: () -> Unit,
) {
    Column(modifier = Modifier.padding(16.dp)) {
        Text("W96D New")
        Text("控制来源: ${state.owner}")
        Text("风速: ${state.speed}%")

        Slider(
            value = state.speed.toFloat(),
            onValueChange = { onSpeed(it.toInt()) },
            valueRange = 0f..100f,
        )

        Button(onClick = onPower) {
            Text(if (state.power) "关闭" else "开启")
        }

        Button(onClick = onNatural) {
            Text("自然风")
        }

        Button(onClick = onTurbo) {
            Text("Turbo")
        }

        Button(onClick = onLight) {
            Text("指示灯")
        }
    }
}
