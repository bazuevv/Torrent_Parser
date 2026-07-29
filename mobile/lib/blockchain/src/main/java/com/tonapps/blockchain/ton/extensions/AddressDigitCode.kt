package com.tonapps.blockchain.ton.extensions

import java.math.BigInteger
import java.util.Base64

private const val ADDRESS_LENGTH = 48
private const val RAW_LENGTH = 36
private const val NUMBER_LENGTH = 87
private const val GROUPS_BEFORE_SHORT_ONE = 11
private const val SHORT_GROUP_START = GROUPS_BEFORE_SHORT_ONE * 4
private const val GROUPS_AFTER_SHORT_ONE_START = SHORT_GROUP_START + 3
private val CODE_GROUP_NUMBERS = intArrayOf(1, 8, 15, 22)

/**
 * Цифровой код TON-адреса — короткая числовая метка для отображения,
 * например `1573-0264-6196-6293`.
 *
 * User-friendly адрес это 48 символов base64url, то есть 36 байт: байт флагов,
 * байт workchain, 32 байта hash part и два байта CRC16-XMODEM. Все 36 байт
 * читаются как одно большое число, дополняются нулями слева до 87 цифр и
 * разбиваются на 22 группы по 4 цифры (двенадцатая из трёх — 87 на 4 не делится).
 * В код берутся группы 1, 8, 15 и 22.
 *
 * Первая группа несёт байты флагов и workchain, поэтому одинакова у всех адресов
 * одной сети (у UQ-адресов basechain это всегда `1573`) — она показывает форму
 * адреса, а не сам кошелёк. Различают кошельки остальные три группы.
 *
 * Код НЕОБРАТИМ: из 87 цифр в нём остаётся 16, восстановить адрес нельзя.
 * Он предназначен только для узнавания и запоминания; сверять адрес перед
 * отправкой средств нужно целиком.
 *
 * @return код адреса либо null, если строка не является user-friendly TON-адресом
 * (например, это адрес TRON или строка повреждена).
 */
fun String.tonAddressDigitCode(): String? {
    val raw = decodeUserFriendlyAddress() ?: return null
    val number = BigInteger(1, raw).toString().padStart(NUMBER_LENGTH, '0')
    return CODE_GROUP_NUMBERS.joinToString("-") { number.digitGroup(it) }
}

/**
 * Возвращает группу [number] (нумерация с единицы) из разметки 87 цифр на 22 группы.
 */
private fun String.digitGroup(number: Int): String {
    val index = number - 1
    return when {
        index < GROUPS_BEFORE_SHORT_ONE -> substring(index * 4, index * 4 + 4)
        index == GROUPS_BEFORE_SHORT_ONE -> substring(SHORT_GROUP_START, GROUPS_AFTER_SHORT_ONE_START)
        else -> {
            val offset = GROUPS_AFTER_SHORT_ONE_START + (index - GROUPS_BEFORE_SHORT_ONE - 1) * 4
            substring(offset, offset + 4)
        }
    }
}

/**
 * Декодирует user-friendly адрес в 36 байт, проверяя длину и CRC.
 * Принимает оба алфавита base64 — url-safe (`-_`) и стандартный (`+/`).
 */
private fun String.decodeUserFriendlyAddress(): ByteArray? {
    val value = trim()
    if (value.length != ADDRESS_LENGTH) {
        return null
    }
    val raw = try {
        Base64.getUrlDecoder().decode(value.replace('+', '-').replace('/', '_'))
    } catch (e: IllegalArgumentException) {
        return null
    }
    if (raw.size != RAW_LENGTH) {
        return null
    }
    val expected = crc16Xmodem(raw, RAW_LENGTH - 2)
    val actual = ((raw[RAW_LENGTH - 2].toInt() and 0xFF) shl 8) or
        (raw[RAW_LENGTH - 1].toInt() and 0xFF)
    return if (expected == actual) raw else null
}

/**
 * CRC16-XMODEM (полином 0x1021, начальное значение 0x0000) по первым [length] байтам.
 */
private fun crc16Xmodem(data: ByteArray, length: Int): Int {
    var crc = 0
    for (i in 0 until length) {
        crc = crc xor ((data[i].toInt() and 0xFF) shl 8)
        repeat(8) {
            crc = if (crc and 0x8000 != 0) {
                ((crc shl 1) xor 0x1021) and 0xFFFF
            } else {
                (crc shl 1) and 0xFFFF
            }
        }
    }
    return crc
}
