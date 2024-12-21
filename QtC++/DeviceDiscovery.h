#include <QObject>
#include <QZeroConf>
#include <QZeroConfService>
#include <QDebug>

class DeviceDiscovery : public QObject {
    Q_OBJECT
public:
    explicit DeviceDiscovery(QObject* parent = nullptr) : QObject(parent) {
        discoverServices();
    }

signals:
    void serviceDiscovered(const QString& name, const QString& ip, int port);
    void serviceLost(const QString& name);

private:
    void discoverServices() {
        QZeroConf zeroConf;

        connect(&zeroConf, &QZeroConf::serviceAdded, this, [this](const QZeroConfService& service) {
            qDebug() << "Service discovered:" << service.name();
            emit serviceDiscovered(service.name(), service.ip().toString(), service.port());
        });

        connect(&zeroConf, &QZeroConf::serviceRemoved, this, [this](const QZeroConfService& service) {
            qDebug() << "Service lost:" << service.name();
            emit serviceLost(service.name());
        });

        if (!zeroConf.startBrowser("_myservice._tcp")) {
            qCritical() << "Failed to start browsing";
        }
    }
};
