package ui.components.moon

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.asPaddingValues
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import ui.theme.Dimens
import ui.theme.UIKit

@Composable
fun moonBottomBarHeight(): Dp {
    val navBarPadding = WindowInsets.navigationBars.asPaddingValues()
    return Dimens.heightActionBar + navBarPadding.calculateBottomPadding()
}

@Composable
fun MoonBottomBar(
    modifier: Modifier = Modifier,
    backgroundColor: Color = UIKit.colorScheme.background.page,
    content: @Composable RowScope.() -> Unit,
) {
    val navBarPadding = WindowInsets.navigationBars.asPaddingValues()
    val navBottom = navBarPadding.calculateBottomPadding()
    val fadeStops = remember(backgroundColor) {
        arrayOf(
            0f to Color.Transparent,
            0.7f to backgroundColor,
        )
    }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(Dimens.heightActionBar + navBottom)
            .background(brush = Brush.verticalGradient(colorStops = fadeStops))
            .padding(bottom = navBottom),
        contentAlignment = Alignment.Center,
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.CenterVertically,
            content = content,
        )
    }
}
