package ui.components.events

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import org.jetbrains.compose.resources.painterResource
import ui.theme.UIKit
import ui.theme.resources.Res
import ui.theme.resources.ic_refresh_16

@Composable
internal fun EventActionRepeat(
    modifier: Modifier = Modifier,
    onClick: () -> Unit
) {
    Box(
        modifier = modifier
            .clip(RoundedCornerShape(18.dp))
            .clickable(onClick = onClick)
            .background(UIKit.colorScheme.background.contentTint)
            .size(36.dp),
        contentAlignment = Alignment.Center
    ) {
        Icon(
            painter = painterResource(Res.drawable.ic_refresh_16),
            contentDescription = null,
            modifier = Modifier.size(16.dp),
            tint = UIKit.colorScheme.accent.blue
        )
    }
}
