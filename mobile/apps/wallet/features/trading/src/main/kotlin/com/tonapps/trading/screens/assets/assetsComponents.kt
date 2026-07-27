package com.tonapps.trading.screens.assets

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import com.tonapps.trading.percentDiffColor
import com.tonapps.uikit.icon.UIKitIcon
import ui.components.moon.MoonItemImage
import ui.components.moon.MoonItemSubtitle
import ui.components.moon.MoonItemTitle
import ui.components.moon.cell.MoonBundleCell
import ui.components.moon.cell.MoonBundlePosition
import ui.components.moon.cell.TextCell
import ui.painterResource
import ui.theme.UIKit

@Composable
internal fun AssetCell(
    item: AssetItem,
    position: MoonBundlePosition = MoonBundlePosition.Default,
    onClick: () -> Unit = {},
) {
    MoonBundleCell(position = position) {
        TextCell(
            title = {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    MoonItemTitle(text = item.symbol)
                    MoonItemTitle(text = item.formattedPrice)
                }
            },
            subtitle = {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    MoonItemSubtitle(text = item.name)
                    MoonItemSubtitle(
                        text = item.formattedChange,
                        color = item.formattedChange.percentDiffColor(),
                    )
                }
            },
            image = {
                MoonItemImage(
                    modifier = Modifier
                        .background(
                            color = UIKit.colorScheme.background.contentTint,
                            shape = CircleShape
                        )
                        .size(44.dp)
                        .clip(CircleShape),
                    image = item.imageUrl,
                    placeholder = painterResource(UIKitIcon.ic_illustration),
                    size = 44.dp
                )
            },
            onClick = onClick,
            minHeight = 76.dp,
        )
    }
}
