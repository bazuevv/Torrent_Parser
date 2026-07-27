package ui.components.events

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import ui.theme.Dimens

@Composable
internal fun EventActionAttachments(
    product: UiEvent.Item.Action.Product?,
    text: UiEvent.Item.Action.Text?,
    canRepeat: Boolean,
    index: Int,
    hiddenBalances: Boolean,
    onClick: (part: EventItemClickPart) -> Unit
) {

    val productClick = remember(index, onClick) { { onClick(EventItemClickPart.Product(index)) } }
    val repeatClick = remember(index, onClick) { { onClick(EventItemClickPart.Repeat(index)) } }

    if (product != null) {
        EventActionProduct(
            modifier = Modifier.padding(
                start = 76.dp,
                top = 8.dp,
                end = Dimens.offsetMedium
            ),
            product = product,
            hiddenBalances = hiddenBalances,
            onClick = productClick
        )
    }

    if (text != null || canRepeat) {
        // Комментарий слева, кнопка повтора прижата к правому краю — под суммой.
        // Box с weight держит её справа и когда комментария нет
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(
                    start = 76.dp,
                    top = 8.dp,
                    end = Dimens.offsetMedium
                ),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Box(modifier = Modifier.weight(1f)) {
                if (text != null) {
                    EventActionText(
                        state = text,
                        index = index,
                        onClick = onClick
                    )
                }
            }

            if (canRepeat) {
                EventActionRepeat(
                    onClick = repeatClick
                )
            }
        }
    }
}