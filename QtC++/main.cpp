#include <QCoreApplication>
#include "Coordinator.h"

int main(int argc, char *argv[]) {
    QCoreApplication app(argc, argv);
    Coordinator coordinator;
    return app.exec();
}
