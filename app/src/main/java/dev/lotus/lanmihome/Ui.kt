package dev.lotus.lanmihome

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

@Composable
fun Section(title:String, content:@Composable ColumnScope.()->Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement=Arrangement.spacedBy(12.dp)) {
            Text(title, style=MaterialTheme.typography.titleMedium, fontWeight=FontWeight.SemiBold)
            content()
        }
    }
}

@Composable
fun ToggleLine(label:String, checked:Boolean, enabled:Boolean=true, note:String?=null, onChange:(Boolean)->Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment=Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(label)
            note?.let { Text(it, style=MaterialTheme.typography.bodySmall, color=MaterialTheme.colorScheme.onSurfaceVariant) }
        }
        Switch(checked=checked, enabled=enabled, onCheckedChange=onChange)
    }
}

@Composable
fun DeviceHeader(title:String, detail:String?, available:Boolean, error:String?) {
    Card(Modifier.fillMaxWidth(), colors=CardDefaults.cardColors(
        containerColor=if(available) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.errorContainer
    )) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement=Arrangement.spacedBy(3.dp)) {
            Text(title, style=MaterialTheme.typography.titleLarge, fontWeight=FontWeight.Bold)
            detail?.let { Text(it, style=MaterialTheme.typography.bodySmall) }
            Text(if(available) "局域网在线" else "不可用", style=MaterialTheme.typography.labelLarge)
            if(!available && !error.isNullOrBlank()) Text(error, style=MaterialTheme.typography.bodySmall)
        }
    }
}
