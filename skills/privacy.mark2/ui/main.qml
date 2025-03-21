import QtQuick.Layouts 1.4
import QtQuick 2.4
import QtQuick.Controls 2.0
import org.kde.kirigami 2.5 as Kirigami
import Mycroft 1.0 as Mycroft

Mycroft.Delegate {
    id: root
    property int gridUnit: Mycroft.Units.gridUnit
    Image {
        anchors.fill: parent
        source: "privacy_gui.png"
        fillMode: Image.PreserveAspectFit
    }
}
