package com.tonapps.deposit.screens.qr

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import com.tonapps.emoji.ui.EmojiView

private const val EMOJI_TO_CIRCLE_RATIO = 24f / 44f

/**
 * Аватар кошелька: эмодзи из его метки на круге цвета метки.
 *
 * Рисуется через [EmojiView] из lib/emoji, а не текстом, потому что метка может
 * содержать не только обычное эмодзи, но и один из встроенных значков
 * (`custom_wallet`, `custom_leaf` и прочие) — их умеет разворачивать только он.
 * Пропорция эмодзи к кругу взята из списка кошельков (24 dp внутри 44 dp).
 *
 * @param emoji значение [com.tonapps.blockchain.model.legacy.Wallet.Label.emoji]
 * @param color цвет метки, ARGB-число из [com.tonapps.blockchain.model.legacy.Wallet.Label.color]
 */
@Composable
fun WalletAvatar(
    emoji: CharSequence,
    color: Int,
    modifier: Modifier = Modifier,
    size: Dp = 44.dp,
) {
    Box(
        modifier = modifier
            .size(size)
            .background(color = Color(color), shape = CircleShape),
        contentAlignment = Alignment.Center
    ) {
        AndroidView(
            modifier = Modifier.size(size * EMOJI_TO_CIRCLE_RATIO),
            factory = { context -> EmojiView(context) },
            update = { view ->
                view.setEmoji(emoji, android.graphics.Color.TRANSPARENT)
            }
        )
    }
}
