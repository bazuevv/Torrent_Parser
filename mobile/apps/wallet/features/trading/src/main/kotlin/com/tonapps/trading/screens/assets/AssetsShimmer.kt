package com.tonapps.trading.screens.assets

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import ui.components.moon.MoonTextShimmer
import ui.components.moon.cell.MoonBundleCell
import ui.components.moon.cell.defaultBundleType
import ui.theme.Dimens
import ui.theme.UIKit

private const val SHIMMER_ROW_COUNT = 10
private const val SHIMMER_PHASE = 0f

private const val ASSET_SYMBOL_PLACEHOLDER = "TONCOIN"
private const val ASSET_PRICE_PLACEHOLDER = "$12,345.67"
private const val ASSET_NAME_PLACEHOLDER = "MicroStrategy xStock"
private const val ASSET_CHANGE_PLACEHOLDER = "+9.99 %"

@Composable
internal fun AssetsShimmer(
    modifier: Modifier = Modifier,
) {
    val fill = UIKit.colorScheme.background.contentTint
    val highlight = UIKit.colorScheme.background.highlighted

    Column(
        modifier = modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .navigationBarsPadding()
            .padding(bottom = 16.dp),
    ) {
        repeat(SHIMMER_ROW_COUNT) { index ->
            MoonBundleCell(position = defaultBundleType(SHIMMER_ROW_COUNT, index)) {
                AssetRowShimmer(
                    fill = fill,
                    highlight = highlight,
                )
            }
        }
    }
}

@Composable
private fun AssetRowShimmer(
    fill: Color,
    highlight: Color,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .defaultMinSize(minHeight = 76.dp)
            .padding(horizontal = Dimens.offsetMedium, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .size(44.dp)
                .clip(CircleShape)
                .background(fill),
        )
        Spacer(modifier = Modifier.width(16.dp))
        Column(
            modifier = Modifier.weight(1f),
            verticalArrangement = Arrangement.Top,
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                MoonTextShimmer(
                    text = ASSET_SYMBOL_PLACEHOLDER,
                    style = UIKit.typography.label1,
                    phase = SHIMMER_PHASE,
                    cornerRadius = 12.dp,
                    backgroundFill = fill,
                    highlightColor = highlight,
                )
                MoonTextShimmer(
                    text = ASSET_PRICE_PLACEHOLDER,
                    style = UIKit.typography.label1,
                    phase = SHIMMER_PHASE,
                    cornerRadius = 12.dp,
                    backgroundFill = fill,
                    highlightColor = highlight,
                )
            }
            Spacer(modifier = Modifier.height(4.dp))
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                MoonTextShimmer(
                    text = ASSET_NAME_PLACEHOLDER,
                    style = UIKit.typography.body2,
                    phase = SHIMMER_PHASE,
                    cornerRadius = 8.dp,
                    backgroundFill = fill,
                    highlightColor = highlight,
                )
                MoonTextShimmer(
                    text = ASSET_CHANGE_PLACEHOLDER,
                    style = UIKit.typography.body2,
                    phase = SHIMMER_PHASE,
                    cornerRadius = 8.dp,
                    backgroundFill = fill,
                    highlightColor = highlight,
                )
            }
        }
    }
}
