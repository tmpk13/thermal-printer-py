package tprint.app;

import java.util.ArrayList;
import java.util.List;

import io.nayuki.qrcodegen.QrCode;

/** QR module matrices for the designer, same shape as the desktop server's segno output. */
final class Qr {
    private Qr() {}

    /** Rows of '0'/'1' (1 = dark), no quiet zone. Always a regular QR code: phone
     *  scanners mostly cannot read Micro QR. */
    static List<String> rows(String data, String error) {
        QrCode.Ecc ecc;
        switch (error.toLowerCase()) {
            case "l": ecc = QrCode.Ecc.LOW; break;
            case "m": ecc = QrCode.Ecc.MEDIUM; break;
            case "q": ecc = QrCode.Ecc.QUARTILE; break;
            case "h": ecc = QrCode.Ecc.HIGH; break;
            default: throw new IllegalArgumentException("unknown error level " + error);
        }
        QrCode qr = QrCode.encodeText(data, ecc); // raises the level when it fits the same size
        List<String> out = new ArrayList<>();
        StringBuilder row = new StringBuilder(qr.size);
        for (int y = 0; y < qr.size; y++) {
            row.setLength(0);
            for (int x = 0; x < qr.size; x++) {
                row.append(qr.getModule(x, y) ? '1' : '0');
            }
            out.add(row.toString());
        }
        return out;
    }
}
